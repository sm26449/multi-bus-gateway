/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The connection form — augments JanitzaMonitor.prototype. One form says how a
// device is reached, in Add Device (prefix devWiz) and on the device page's
// Connection tab (prefix ddv). Its model and what it sends are
// ConnformModel's (connform-model.js); this draws it, reads it back, places
// the gateway's refusals at their fields and runs its Test.
//
//   _cfMount(host, px, M, opts) → the form; this._cf[px] is the live one
//   opts: { context: 'add'|'edit', lockClass, lockMode, primary, allowNone,
//           deviceId, template: () => id, onChange }
Object.assign(JanitzaMonitor.prototype, {

    _cfMount(host, px, M, opts = {}) {
        const CM = window.ConnformModel;
        const form = {
            px, opts, host,
            M: JSON.parse(JSON.stringify(M)),
            orig: JSON.parse(JSON.stringify(M)),
            shown: null,
        };
        form.M.keepalive = M.keepalive;
        form.orig.keepalive = M.keepalive;
        form.read = () => {
            form.M = CM.apply(form.M, this._cfInputs(form), form.shown);
            let errors = CM.errors(form.M, (k, d) => this.t(k, d));
            if (opts.context === 'edit') {
                // on a saved device only what is being changed can block a
                // save: a stored value the form would judge differently
                // never stops a rename
                // (a value changed, not merely sent: the primary sends its whole
                // address, and a stored sub-second timeout must not block a
                // new host)
                const changed = new Set(CM.changed(form.M, form.orig));
                errors = Object.fromEntries(Object.entries(errors)
                    .filter(([p]) => changed.has(p.replace(/^connection\./, '').split('.')[0])));
            }
            return { model: form.M, errors };
        };
        form.payload = () => CM.payload(form.M, form.orig, { context: opts.context, primary: !!opts.primary });
        form.wire = () => CM.wire(form.M);
        form.testConnection = () => CM.testConnection(form.M, opts.context === 'edit' ? form.orig : null);
        form.dirty = () => Object.keys(CM.payload(form.M, form.orig, { context: 'edit', primary: !!opts.primary })).length > 0;
        form.fieldId = path => this._cfFieldId(form, path);
        form.showErrors = fields => this._placeFieldErrors(host, fields, p => form.fieldId(p));
        this._cf = this._cf || {};
        this._cf[px] = form;
        // a new device opens on a bridge its mode can use (RTU over the
        // network starts on the first bridge there is — unless an address
        // was typed instead: M.legacy, kept across Back/Next)
        if (opts.context !== 'edit') this._cfFitBridge(form);
        this._cfRender(form);
        return form;
    },

    // draw (or redraw after a change of mode or bridge) and remember what it
    // shows: a field counts as edited only when it differs from that
    _cfRender(form, focusId) {
        form.host.innerHTML = this._cfHtml(form);
        form.shown = this._cfInputs(form);
        // on a saved device, a change of how it is reached (UDP, framing, a
        // bridge left) is said beside it as soon as it is made
        if (form.opts.context === 'edit' && !form.host._cfWatch) {
            form.host._cfWatch = true;
            form.host.addEventListener('change', () => {
                const f = (this._cf || {})[form.px];
                const hint = document.getElementById(form.px + 'RouteHint');
                if (!f || !hint) return;
                const M = window.ConnformModel.apply(f.M, this._cfInputs(f), f.shown);
                const note = this._cfRouteNote(M, f.orig);
                hint.hidden = !note;
                const txt = hint.querySelector('span');
                if (txt && note) txt.textContent = note;
            });
        }
        if (focusId) document.getElementById(focusId)?.focus();
        form.opts.onChange?.(form);
    },

    // read what is on screen, then redraw (a mode, a bridge, a bus picked)
    _cfRerender(px, el) {
        const form = (this._cf || {})[px];
        if (!form) return;
        form.read();
        if (el && el.name === `${px}Proto`) {
            // RTU opens on its recommended mode, over the network
            form.M.mode = el.value === 'rtu' ? (form.orig.mode === 'rtu' || form.orig.mode === 'rtu_tap' ? form.orig.mode : 'rtu-tcp') : el.value;
            this._cfFitBridge(form);
        } else if (el && el.name === `${px}RtuMode`) {
            form.M.mode = el.value;
            this._cfFitBridge(form);
        } else if (el && el.id === `${px}Bridge`) {
            const b = (this._bridges || []).find(x => x.id === el.value);
            form.M.bridge = el.value;
            form.M.bridge_port = ((b?.ports || [])[0] || {}).port ?? null;
        }
        this._cfRender(form, el?.id || null);
    },

    // a bridge chosen in one mode that the new mode cannot use is dropped:
    // TCP goes through a gateway, RTU over the network and listening need RTU
    _cfFitBridge(form) {
        const M = form.M;
        if (!M.bridge) {
            // RTU over the network starts on the first bridge there is
            if (M.mode === 'rtu-tcp' && !M.legacy && this._rtuBridges().length) {
                const b = this._rtuBridges()[0];
                M.bridge = b.id;
                M.bridge_port = ((b.ports || [])[0] || {}).port ?? null;
            }
            return;
        }
        const gw = this._bridgeType(((this._bridges || []).find(b => b.id === M.bridge) || {}).type).framing === 'modbus_tcp';
        const ok = M.mode === 'tcp' ? gw : (M.mode === 'rtu-tcp' || M.mode === 'rtu_tap') ? !gw : false;
        if (!ok) { M.bridge = ''; M.bridge_port = null; }
    },

    // bridges that pass RTU frames (a Modbus TCP gateway is reached as TCP)
    _rtuBridges() {
        return (this._bridges || []).filter(b => this._bridgeType(b.type).framing !== 'modbus_tcp');
    },

    // ── reading it back ─────────────────────────────────────────────────────
    // the field keys of what the form shows now (never a hidden mode's fields)
    _cfInputs(form) {
        const px = form.px, M = form.M;
        const g = id => document.getElementById(px + id);
        const val = id => g(id)?.value;
        const out = {};
        const put = (k, id, check = false) => {
            const el = g(id);
            if (!el) return;
            out[k] = check ? !!el.checked : el.value;
        };
        const proto = document.querySelector(`input[name="${px}Proto"]:checked`)?.value;
        const rtuMode = document.querySelector(`input[name="${px}RtuMode"]:checked`)?.value;
        out.mode = proto === 'rtu' ? (rtuMode || M.mode) : (proto || M.mode);
        switch (M.mode) {
        case 'tcp':
            put('via', 'Gw');
            if (!M.bridge) { put('host', 'Host'); put('port', 'Port'); put('udp', 'Udp', true); }
            put('unit_id', 'Unit'); put('timeout', 'Timeout');
            break;
        case 'rtu-tcp':
            if (g('Bridge')) {
                out.bridge = val('Bridge') || '';
                put('bridge_port', 'BridgePort');
                put('unit_id', 'UnitBr'); put('timeout', 'TimeoutBr');
            } else {
                put('host', 'RtuHost'); put('port', 'RtuPort'); put('unit_id', 'UnitB'); put('timeout', 'TimeoutB');
                put('framing', 'FramingB');
            }
            break;
        case 'rtu':
        case 'rtu_tap':
            put('via', 'TapWhere');
            if (!M.bridge) {
                put('serial_port', 'Serial');
                put('baudrate', 'Baud'); put('parity', 'Parity'); put('stopbits', 'Stop'); put('bytesize', 'Bits');
            }
            if (M.mode === 'rtu') { put('framing', 'Framing'); put('timeout', 'TimeoutR'); }
            put('unit_id', 'UnitR');
            break;
        case 'http':
            put('url', 'Url');
            break;
        case 'mqtt':
            put('broker', 'Broker'); put('mqtt_port', 'MqttPort'); put('topic', 'MqttTopic'); put('vars', 'MqttVars');
            put('username', 'MqttUser'); put('password', 'MqttPass');
            put('tls', 'MqttTls', true); put('tls_insecure', 'MqttTlsInsecure', true);
            put('ka_mode', 'KaMode'); put('ka_topic', 'KaTopic'); put('ka_first', 'KaFirst');
            put('ka_payload', 'KaPayload'); put('ka_every', 'KaEvery');
            break;
        default:
            break;
        }
        return out;
    },

    // a server path → the input that holds it, in the mode shown
    _cfFieldId(form, path) {
        const CM = window.ConnformModel;
        const M = form.M, px = form.px;
        if (path === 'id' || path === 'name' || path === 'template') return null;
        const key = CM.fieldOf(path, M);
        if (!key) return null;
        const legacy = M.mode === 'rtu-tcp' && !M.bridge;
        const id = {
            via: M.mode === 'tcp' ? 'Gw' : 'TapWhere',
            bridge: 'Bridge', bridge_port: 'BridgePort',
            host: legacy ? 'RtuHost' : 'Host',
            port: legacy ? 'RtuPort' : 'Port',
            mqtt_port: 'MqttPort',
            unit_id: M.mode === 'tcp' ? 'Unit' : M.mode === 'rtu-tcp' ? (legacy ? 'UnitB' : 'UnitBr') : 'UnitR',
            timeout: M.mode === 'tcp' ? 'Timeout' : M.mode === 'rtu-tcp' ? (legacy ? 'TimeoutB' : 'TimeoutBr') : 'TimeoutR',
            serial_port: 'Serial', baudrate: 'Baud', parity: 'Parity', stopbits: 'Stop', bytesize: 'Bits',
            framing: legacy ? 'FramingB' : 'Framing',
            url: 'Url', broker: 'Broker', topic: 'MqttTopic', username: 'MqttUser', password: 'MqttPass',  // leak-check: ok — field ids
            tls: 'MqttTls', tls_insecure: 'MqttTlsInsecure', vars: 'MqttVars',
            ka_mode: 'KaMode', ka_topic: 'KaTopic', ka_every: 'KaEvery',
        }[key];
        return id && document.getElementById(px + id) ? px + id : null;
    },

    // ── drawing it ─────────────────────────────────────────────────────────
    _cfHtml(form) {
        const CM = window.ConnformModel;
        const { px, M, opts } = form;
        const t = (k, d, p) => this.t(k, d, p);
        const cls = CM.classOf;
        const lockClass = opts.lockClass || null;
        const isRtu = ['rtu', 'rtu-tcp', 'rtu_tap'].includes(M.mode);
        // what may not be chosen: the primary is Modbus TCP; a device holding
        // components keeps how it is reached; an edit keeps its class
        const off = p => opts.primary ? p !== 'tcp'
            : opts.lockMode ? p !== (isRtu ? 'rtu' : M.mode)
            : !!lockClass && cls(p) !== lockClass;
        const seg = (value, label, on) => `<label class="seg-btn ${on ? 'on' : ''} ${off(value) ? 'disabled' : ''}"><input type="radio" id="${px}Proto-${value}" name="${px}Proto" value="${value}" ${on ? 'checked' : ''} ${off(value) ? 'disabled' : ''} ${this._act('_cfRerender', [px], { el: true, on: 'change' })}><span class="s"></span> ${label}</label>`;
        const lockHint = opts.primary
            ? t('cf.primaryLock', 'The primary meter is read over Modbus TCP: its address may change, not how it is reached.')
            : opts.lockMode ? t('cf.holdsLock', 'It holds components, which are read through it: how it is reached stays as it is.')
            : lockClass === 'modbus' ? t('devices.wizard.protoModbusOnly', 'A Modbus device can move between Modbus TCP, RTU, over the network or listen-only — its register map stays valid. HTTP and MQTT read by path, so they need a new device.')
            : lockClass ? t('devices.wizard.protoLocked', 'Fixed after creation — the template map is transport-specific.') : '';
        const routeNote = opts.context === 'edit' ? this._cfRouteNote(M, form.orig) : '';
        const test = this._cfTestHtml(form);
        return `
        <fieldset class="cf-fieldset">
        <legend class="wiz-eyebrow">${t('devices.wizard.protoQ', 'How is it connected?')}</legend>
        <div class="seg" role="radiogroup" aria-label="${this._esc(t('devices.wizard.protocol', 'Protocol'))}">
            ${seg('tcp', 'Modbus TCP', M.mode === 'tcp')}
            ${seg('rtu', 'Modbus RTU', isRtu)}
            ${seg('http', 'HTTP / JSON', M.mode === 'http')}
            ${seg('mqtt', 'MQTT', M.mode === 'mqtt')}
            ${opts.allowNone ? seg('none', this._esc(t('wizMtr.segShort', 'Meters only')), M.mode === 'none') : ''}
        </div>
        ${opts.allowNone ? `<div class="field-hint cf-note">${this._esc(t('wizMtr.segHint', 'Meters only: no connection of its own — it is measured by meters you already have.'))}</div>` : ''}
        ${lockHint ? `<div class="field-hint cf-lock"><i aria-hidden="true" class="bi bi-lock"></i> ${lockHint}</div>` : ''}
        <div class="field-hint cf-route" id="${px}RouteHint" role="status" ${routeNote ? '' : 'hidden'}><i aria-hidden="true" class="bi bi-arrow-repeat"></i> <span>${this._esc(routeNote)}</span></div>
        </fieldset>
        ${M.mode === 'none' ? `<div id="${px}NoneFields"><p class="field-hint"><i aria-hidden="true" class="bi bi-lightning"></i> ${t('wizMtr.lead', 'For equipment with no communication of its own (a UPS, a charger, a boiler): it is measured by meters you already have — one on its supply, or one on what goes in and one on what comes out. Next you say what it is and which meters measure it.')}</p></div>` : ''}
        ${M.mode === 'http' ? this._cfHttpHtml(form, test) : ''}
        ${M.mode === 'tcp' ? this._cfTcpHtml(form, test) : ''}
        ${isRtu ? this._cfRtuHtml(form, test) : ''}
        ${M.mode === 'mqtt' ? this._cfMqttHtml(form, test) : ''}`;
    },

    // what a save of M would do to how it is reached, said beside the choice:
    // another protocol starts afresh; onto or off a bridge keeps the rest
    _cfRouteNote(M, orig) {
        const CM = window.ConnformModel;
        const t = (k, d) => this.t(k, d);
        if (CM.routeChanged(M, orig)) return t('cf.routeChanged', 'Changing how it is reached starts its connection settings afresh: what this form does not show (retries, tuning) goes back to the defaults.');
        if (CM.bridgeMoved(M, orig)) {
            return CM.bridgeOf(M)
                ? t('cf.ontoBridge', 'It will be reached through the bridge: its own address and line settings are cleared (the bridge has them); everything else is kept.')
                : t('cf.offBridge', 'It leaves the bridge and gets an address of its own; everything else is kept.');
        }
        return '';
    },

    _cfTestHtml(form) {
        if (form.M.mode === 'none') return '';
        // the action names the forms always had (e2e and docs refer to them)
        const action = form.px === 'devWiz' ? 'devWizardTest' : 'deviceDetailTest';
        return `<div class="cf-test"><button type="button" data-live class="btn btn-secondary btn-sm" id="${form.px}TestBtn" data-action="${action}" data-with-el>
                <i aria-hidden="true" class="bi bi-activity"></i> ${this.t('devices.wizard.testConn', 'Test connection')}</button>
            <span class="wiz-test-result" id="${form.px}TestResult" role="status" aria-live="polite"></span></div>`;
    },

    _cfNum(px, id, label, value, min, max, hint) {
        return `<div class="form-group"><label class="form-label" for="${px}${id}">${label}</label>
            <input type="number" id="${px}${id}" class="input" value="${this._esc(value ?? '')}" min="${min}" max="${max}" inputmode="numeric">
            ${hint ? `<div class="field-hint">${hint}</div>` : ''}</div>`;
    },
    _cfUnit(px, id, M) {
        return this._cfNum(px, id, this.t('lbl.unitId', 'Unit ID'), M.unit_id, 0, 255, `0–255 · ${this.t('common.default', 'default')} 1`);
    },
    _cfTimeout(px, id, M) {
        return this._cfNum(px, id, this.t('lbl.timeoutS', 'Timeout (s)'), M.timeout ?? 3, 1, 30, `1–30 · ${this.t('common.default', 'default')} 3`);
    },

    _cfHttpHtml(form, test) {
        const { px, M } = form;
        const t = (k, d) => this.t(k, d);
        return `<fieldset class="cf-fieldset" id="${px}HttpFields"><legend class="sr-only">HTTP / JSON</legend>
            <div class="form-group">
                <label class="form-label" for="${px}Url">${t('devices.wizard.httpUrl', 'JSON endpoint URL')}</label>
                <input type="url" id="${px}Url" class="input" value="${this._esc(M.url || '')}" placeholder="http://192.168.1.50/rpc/Shelly.GetStatus" spellcheck="false">
                <div class="field-hint">${t('devices.wizard.httpHint', 'The device polls this URL and reads values by the template’s json_path. Values arrive already scaled.')}</div>
            </div>${test}</fieldset>`;
    },

    _cfTcpHtml(form, test) {
        const { px, M, opts } = form;
        const t = (k, d) => this.t(k, d);
        const bridged = !!M.bridge;
        return `<fieldset class="cf-fieldset" id="${px}TcpFields"><legend class="sr-only">Modbus TCP</legend>
            ${opts.primary ? '' : this._cfGatewayPickHtml(form)}
            <div class="form-row">
                ${bridged ? '' : `<div class="form-group flex-2"><label class="form-label" for="${px}Host">${t('lbl.hostIp', 'Host / IP')}</label>
                    <input type="text" id="${px}Host" class="input" value="${this._esc(M.host)}" placeholder="192.168.1.60" spellcheck="false" autocomplete="off"></div>
                ${this._cfNum(px, 'Port', t('lbl.port', 'Port'), M.port ?? 502, 1, 65535, `1–65535 · ${t('common.default', 'default')} 502`)}`}
                ${this._cfUnit(px, 'Unit', M)}
                ${this._cfTimeout(px, 'Timeout', M)}
            </div>
            ${bridged || opts.primary ? '' : `<label class="checkbox-label cf-check ${opts.lockMode ? 'disabled' : ''}" title="${this._esc(t('devices.wizard.udpHint', 'Some devices and gateways answer Modbus TCP frames in UDP datagrams (usually port 502) instead of a TCP connection. Use it only when the manual says UDP.'))}">
                <input type="checkbox" id="${px}Udp" ${M.udp ? 'checked' : ''} ${opts.lockMode ? 'disabled' : ''}> <span>${t('devices.wizard.udp', 'Over UDP (datagrams instead of a connection)')}</span></label>`}
            ${test}</fieldset>`;
    },

    _cfRtuHtml(form, test) {
        const { px, M, opts } = form;
        const t = (k, d) => this.t(k, d);
        const rtuBridge = M.mode === 'rtu-tcp';
        const tap = M.mode === 'rtu_tap';
        const tapNet = tap && !!M.bridge;
        const mode = (value, label, extra = '') => `<label class="seg-btn ${M.mode === value ? 'on' : ''}"><input type="radio" id="${px}RtuMode-${value}" name="${px}RtuMode" value="${value}" ${M.mode === value ? 'checked' : ''} ${opts.lockMode && M.mode !== value ? 'disabled' : ''} ${this._act('_cfRerender', [px], { el: true, on: 'change' })}><span class="s"></span> ${label}${extra}</label>`;
        let body;
        if (rtuBridge) {
            const legacy = !M.bridge;
            const rtuBridges = this._rtuBridges().length;
            body = `<div id="${px}RtuBridge">
                ${legacy ? (rtuBridges && !opts.lockMode ? `<div class="field-hint cf-note"><a href="#" ${this._act('_cfUseBridge', [px])}>${t('cf.useBridge', 'Reach it through a bridge instead')}</a></div>`
                                       : this._cfBridgePickHtml(form)) : this._cfBridgePickHtml(form)}
                <div id="${px}LegacyScan" ${legacy ? '' : 'hidden'}>${legacy ? this._cfLegacyHtml(form) : ''}</div>
                ${test}</div>`;
        } else {
            body = `<div id="${px}RtuDirect">
                ${tap ? this._cfTapWhereHtml(form) : ''}
                <div class="form-row">
                    ${tapNet ? '' : `<div class="form-group flex-2"><label class="form-label" for="${px}Serial">${t('devices.wizard.serialPort', 'Serial port')}</label>
                        <input type="text" id="${px}Serial" class="input" value="${this._esc(M.serial_port)}" placeholder="/dev/ttyUSB0" spellcheck="false" autocomplete="off"></div>`}
                    ${tap ? '' : `<div class="form-group"><label class="form-label" for="${px}Framing">${t('devices.wizard.framing', 'Framing')}</label>
                        <select id="${px}Framing" class="input" title="${this._esc(t('devices.wizard.framingHint', 'RTU (binary, CRC) is what almost every device speaks. ASCII (“:” … LRC, readable hex) is found on older PLCs and meters — use it only when the manual says Modbus ASCII. All slaves on one line speak the same one.'))}">
                            <option value="rtu" ${M.framing !== 'ascii' ? 'selected' : ''}>RTU</option>
                            <option value="ascii" ${M.framing === 'ascii' ? 'selected' : ''}>ASCII</option></select></div>`}
                    ${this._cfUnit(px, 'UnitR', M)}
                    ${tap ? '' : this._cfTimeout(px, 'TimeoutR', M)}
                </div>
                ${tapNet ? '' : this._serialLineHtml(px, M)}
                ${tapNet ? `<div class="field-hint cf-note"><i aria-hidden="true" class="bi bi-ear"></i> ${t('devices.wizard.rtuTapNetNote', 'Listen-only through the bridge: another master already polls this bus — the gateway never transmits, it decodes the answers that unit gives, at the master’s rhythm. Several taps can listen to one bus, each with its own unit id; a bus is either polled or tapped. After saving, Test on the device shows what the tap hears.')}</div>`
                : tap ? `<div class="field-hint cf-note"><i aria-hidden="true" class="bi bi-ear"></i> ${t('devices.wizard.rtuTapNote', 'Listen-only: another master already polls this bus — the gateway never transmits, it decodes the answers that unit gives. Values arrive at the master’s rhythm; several taps can share one port (same baud/parity). The adapter must be mapped into the container (e.g. devices: /dev/ttyUSB1). After saving, Test on the device shows what the tap hears.')}</div>`
                : `<div class="field-hint cf-note">${t('devices.wizard.rtuNote', 'The serial adapter must be attached to the host and mapped into the container (e.g. devices: /dev/ttyUSB0:/dev/ttyUSB0). Polling starts right after saving.')}</div>`}
                ${tap && form.opts.context === 'add' ? '' : test}</div>`;
        }
        return `<fieldset class="cf-fieldset" id="${px}RtuFields"><legend class="sr-only">Modbus RTU</legend>
            <div class="seg seg-sm" role="radiogroup" aria-label="${this._esc(t('devices.wizard.rtuMode', 'RTU connection mode'))}">
                ${mode('rtu-tcp', t('devices.wizard.rtuOverNet', 'Over network (auto-detect)'), ` <span class="seg-reco">${t('common.recommended', 'recommended')}</span>`)}
                ${mode('rtu', t('devices.wizard.rtuDirect', 'Direct serial'))}
                ${mode('rtu_tap', t('devices.wizard.rtuTap', 'Listen only (tap)'))}
            </div>
            ${body}</fieldset>`;
    },

    // RTU over the network without a bridge record: its address typed, or
    // found by a scan of the serial bridge next to the gateway
    _cfLegacyHtml(form) {
        const { px, M } = form;
        const t = (k, d) => this.t(k, d);
        return `<div class="form-row align-end">
                <div class="form-group flex-2">
                    <label class="form-label" for="${px}Adapter">${t('devices.wizard.adapter', 'USB serial adapter')}</label>
                    <select id="${px}Adapter" class="input" ${this._act('_cfAdapterPick', [px], { value: true, on: 'change' })}>${this._cfAdapterOptions(form)}</select>
                </div>
                <div class="form-group">
                    <button type="button" class="btn btn-secondary btn-sm" id="${px}ScanBtn" ${this._act('_cfScanBridge', [px], { el: true })}>
                        <i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${t('devices.wizard.scan', 'Scan')}</button>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="${px}RtuHost">${t('cf.bridgeHost', 'Bridge address')}</label>
                    <input type="text" id="${px}RtuHost" class="input" value="${this._esc(M.host)}" placeholder="192.168.1.80" spellcheck="false" autocomplete="off"></div>
                ${this._cfNum(px, 'RtuPort', t('lbl.port', 'Port'), M.port ?? '', 1, 65535, '')}
                ${this._cfUnit(px, 'UnitB', M)}
                ${this._cfTimeout(px, 'TimeoutB', M)}
                <div class="form-group"><label class="form-label" for="${px}FramingB">${t('devices.wizard.framing', 'Framing')}</label>
                    <select id="${px}FramingB" class="input">
                        <option value="rtu" ${M.framing !== 'ascii' ? 'selected' : ''}>RTU</option>
                        <option value="ascii" ${M.framing === 'ascii' ? 'selected' : ''}>ASCII</option></select></div>
            </div>
            <div class="field-hint" id="${px}BridgeHint">${M.host
                ? this._esc(t('devices.wizard.bridgeBound', 'Bound to {ep} — the adapter keeps this endpoint across replug.').replace('{ep}', `${M.host}:${M.port ?? ''}`))
                : t('devices.wizard.bridgeScanHint', 'Plug the adapter in and press Scan. Baud rate is set on the bridge (default 9600 8N1).')}</div>`;
    },

    _cfAdapterOptions(form) {
        const M = form.M;
        const ads = (form.adapters || []).filter(a => a.available && a.tcp_port);
        const cur = (M.host && M.port) ? `${M.host}::${M.port}` : '';
        if (!ads.length && !cur) return `<option value="">${this._esc(this.t('devices.wizard.noAdapters', '— press Scan to detect adapters —'))}</option>`;
        const opts = ads.map(a => {
            const v = `${a.bridge_host}::${a.tcp_port}`;
            const label = `${a.model || a.manufacturer || a.vendor_id || 'serial'}${a.serial ? ' (' + a.serial + ')' : ''} → :${a.tcp_port}`;
            return `<option value="${this._esc(v)}" ${v === cur ? 'selected' : ''}>${this._esc(label)}</option>`;
        });
        if (cur && !ads.some(a => `${a.bridge_host}::${a.tcp_port}` === cur)) {
            opts.unshift(`<option value="${this._esc(cur)}" selected>${this._esc(`${M.host}:${M.port}`)}</option>`);
        }
        return opts.join('');
    },

    // a picked adapter fills the address it is reached at
    _cfAdapterPick(px, value) {
        const [host, port] = String(value).split('::');
        const h = document.getElementById(px + 'RtuHost');
        const p = document.getElementById(px + 'RtuPort');
        if (h && host) h.value = host;
        if (p && port) p.value = port;
        const hint = document.getElementById(px + 'BridgeHint');
        if (hint && host) hint.textContent = this.t('devices.wizard.bridgeBound', 'Bound to {ep} — the adapter keeps this endpoint across replug.').replace('{ep}', `${host}:${port}`);
    },

    async _cfScanBridge(px, btn) {
        const form = (this._cf || {})[px];
        if (!form) return;
        btn.disabled = true;
        let msg = '', ok = false;
        try {
            const r = await fetch('/api/bridge/adapters');
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [data.detail || r.statusText]).join('; '));
            if (!data.available) throw new Error(data.error || this.t('devices.wizard.bridgeDown', 'Serial bridge not reachable.'));
            form.adapters = data.adapters || [];
            const avail = form.adapters.filter(a => a.available && a.tcp_port);
            form.read();
            if (avail.length === 1) { form.M.host = avail[0].bridge_host; form.M.port = avail[0].tcp_port; }
            ok = avail.length > 0;
            msg = avail.length ? this.tn('devices.wizard.scanFound', avail.length, 'Found {n} adapters.')
                : this.t('devices.wizard.scanNone', 'No available adapters — plug one in and Scan again.');
        } catch (e) { msg = String(e.message || e); }
        this._cfRender(form, `${px}ScanBtn`);
        const out = document.getElementById(px + 'TestResult');
        if (out) { out.className = 'wiz-test-result ' + (ok ? 'ok' : 'err'); out.textContent = (ok ? '✓ ' : '✗ ') + msg; }
    },

    // Modbus TCP: directly, or through a Modbus TCP gateway's bus
    _cfGatewayPickHtml(form) {
        const { px, M, opts } = form;
        const t = (k, d, p) => this.t(k, d, p);
        const buses = [];
        for (const b of this._bridges || []) {
            if (this._bridgeType(b.type).framing !== 'modbus_tcp') continue;
            for (const p of b.ports || []) buses.push({ b, p });
        }
        const cur = M.bridge ? `${M.bridge}::${M.bridge_port ?? ((buses.find(x => x.b.id === M.bridge) || {}).p || {}).port ?? ''}` : '';
        if (M.bridge && !buses.some(({ b, p }) => `${b.id}::${p.port}` === cur)) {
            buses.unshift({ b: { id: M.bridge, name: `${M.bridge} (${t('devices.missingBridge', 'not found')})`, host: '' }, p: { port: M.bridge_port ?? '' } });
        }
        if (!buses.length) return '';
        return `<div class="form-row"><div class="form-group flex-2">
            <label class="form-label" for="${px}Gw">${t('devices.wizard.gwWhere', 'Reached')}</label>
            <select id="${px}Gw" class="input" ${form.opts.lockMode ? 'disabled' : ''} ${this._act('_cfRerender', [px], { el: true, on: 'change' })}>
              <option value="" ${cur ? '' : 'selected'}>${t('devices.wizard.gwDirect', 'Directly — its own IP and port')}</option>
              ${buses.map(({ b, p }) => {
                  const v = `${b.id}::${p.port}`;
                  const n = (p.devices || []).filter(x => x.id !== opts.deviceId).length;
                  return `<option value="${this._esc(v)}" ${v === cur ? 'selected' : ''}>${this._esc(t('devices.wizard.gwVia', 'Through the gateway {name} · {host}:{port}', { name: b.name || b.id, host: b.host, port: p.port }))}${n ? ' · ' + this._esc(t('bridges.nDevices', '{n} device(s)', { n })) : ''}</option>`;
              }).join('')}
            </select>
            <div class="field-hint">${t('devices.wizard.gwHint', 'An RS-485 slave behind a converter in “Modbus TCP to RTU” mode: pick the gateway, then the slave’s unit ID. The gateway’s address and connection limit come from the bridge.')}${opts.context === 'edit' && M.bridge ? ' ' + t('devices.busMoveHint', 'Pick another bus to move it there; its topics and history stay.') : ''}</div>
          </div></div>`;
    },

    // RTU over the network: which bridge, which of its buses
    _cfBridgePickHtml(form) {
        const { px, M, opts } = form;
        const t = (k, d, p) => this.t(k, d, p);
        const bridges = this._rtuBridges().slice();
        if (M.bridge && !bridges.some(b => b.id === M.bridge)) {
            bridges.unshift({ id: M.bridge, name: `${M.bridge} (${t('devices.missingBridge', 'not found')})`, host: '', ports: M.bridge_port ? [{ port: M.bridge_port }] : [] });
        }
        if (!bridges.length) {
            return `<div class="field-hint cf-note">${t('devices.wizard.noBridge', 'No bridge yet. A bridge is the box the bus is reached through — our serial bridge on a Raspberry Pi or server, or an RS-485-to-Ethernet converter (Waveshare, USR, Elfin…).')}
                <a href="#" ${this._act('openBridgeModal', [])}>${t('bridges.add', 'Add bridge')}</a></div>`;
        }
        const cur = bridges.find(b => b.id === M.bridge) || bridges[0];
        const ports = (cur.ports || []).slice();
        if (M.bridge_port != null && !ports.some(p => +p.port === +M.bridge_port)) ports.unshift({ port: M.bridge_port });
        const port = M.bridge_port ?? (ports[0] || {}).port;
        const used = (ports.find(p => +p.port === +port)?.devices || []).filter(x => x.id !== opts.deviceId);
        return `<div class="form-row align-end" id="${px}BridgeRow">
            <div class="form-group flex-2"><label class="form-label" for="${px}Bridge">${t('devices.wizard.bridge', 'Bridge')}</label>
              <select id="${px}Bridge" class="input" ${this._act('_cfRerender', [px], { el: true, on: 'change' })}>
                ${bridges.map(b => `<option value="${this._esc(b.id)}" ${b.id === cur.id ? 'selected' : ''}>${this._esc(b.name || b.id)}${b.host ? ' · ' + this._esc(b.host) : ''}</option>`).join('')}
              </select></div>
            <div class="form-group"><label class="form-label" for="${px}BridgePort">${t('devices.wizard.bus', 'Bus')}</label>
              <select id="${px}BridgePort" class="input" ${this._act('_cfRerender', [px], { el: true, on: 'change' })}>
                ${ports.map(p => `<option value="${this._esc(p.port)}" ${+p.port === +port ? 'selected' : ''}>:${this._esc(p.port)}${p.label ? ' · ' + this._esc(p.label) : ''}</option>`).join('')
                  || `<option value="">${t('devices.wizard.noBus', 'no bus yet')}</option>`}
              </select></div>
            ${this._cfUnit(px, 'UnitBr', M)}
            ${this._cfTimeout(px, 'TimeoutBr', M)}
          </div>
          <div class="field-hint cf-note" id="${px}BridgeHint2">${used.length
              ? this._esc(t('devices.wizard.busHas', 'Also on this bus: {list} — each slave needs its own unit id; they take turns on one connection.', { list: used.map(x => `${x.name || x.id} (unit ${x.unit_id})`).join(', ') }))
              : this._esc(t('devices.wizard.busEmpty', 'First device on this bus.'))}
            ${opts.context === 'edit' ? ' ' + this._esc(t('devices.busMoveHint', 'Pick another bus to move it there; its topics and history stay.')) : ''}
            ${opts.lockMode ? '' : `· <a href="#" ${this._act('_cfLeaveBridge', [px])}>${opts.context === 'edit' ? t('devices.offBridge', 'Take it off the bridge…') : t('devices.wizard.legacyScan', 'scan a bridge next to the gateway instead')}</a>`}</div>`;
    },

    // RTU over the network, not through a bridge record: its address typed
    // or scanned (on a saved device: it leaves the bridge — said on save)
    _cfLeaveBridge(px) {
        const form = (this._cf || {})[px];
        if (!form) return;
        form.read();
        form.M.bridge = '';
        form.M.bridge_port = null;
        form.M.legacy = true;
        this._cfRender(form, `${px}RtuHost`);
    },

    // back from a typed address to a bridge's bus
    _cfUseBridge(px) {
        const form = (this._cf || {})[px];
        if (!form) return;
        form.read();
        form.M.legacy = false;
        const b = this._rtuBridges()[0];
        if (b) { form.M.bridge = b.id; form.M.bridge_port = ((b.ports || [])[0] || {}).port ?? null; }
        this._cfRender(form, `${px}Bridge`);
    },

    // listen-only: where the bus is heard — a serial port on this host, or the
    // bus of a bridge that passes the bytes on (a gateway only answers questions)
    _cfTapWhereHtml(form) {
        const { px, M } = form;
        const t = (k, d) => this.t(k, d);
        const buses = [];
        for (const b of this._bridges || []) {
            if (this._bridgeType(b.type).framing === 'modbus_tcp') continue;
            for (const p of b.ports || []) buses.push({ b, p });
        }
        const cur = M.bridge ? `${M.bridge}::${M.bridge_port ?? ''}` : '';
        if (M.bridge && !buses.some(({ b, p }) => `${b.id}::${p.port}` === cur)) {
            buses.unshift({ b: { id: M.bridge, name: `${M.bridge} (${t('devices.missingBridge', 'not found')})`, host: '' }, p: { port: M.bridge_port ?? '' } });
        }
        if (!buses.length) return '';
        return `<div class="form-row"><div class="form-group flex-2">
            <label class="form-label" for="${px}TapWhere">${t('devices.wizard.tapWhere', 'Where the bus is')}</label>
            <select id="${px}TapWhere" class="input" ${form.opts.lockMode ? 'disabled' : ''} ${this._act('_cfRerender', [px], { el: true, on: 'change' })}>
              <option value="" ${cur ? '' : 'selected'}>${t('devices.wizard.tapLocal', 'A serial port on this host')}</option>
              ${buses.map(({ b, p }) => {
                  const v = `${b.id}::${p.port}`;
                  return `<option value="${this._esc(v)}" ${v === cur ? 'selected' : ''}>${this._esc(b.name || b.id)}${b.host ? ' · ' + this._esc(b.host) : ''}:${this._esc(p.port)}${p.label ? ' · ' + this._esc(p.label) : ''}</option>`;
              }).join('')}
            </select>
            <div class="field-hint">${t('devices.wizard.tapWhereHint', 'Through a bridge, the gateway reads the bridge’s port and never writes to it. Only transparent bridges and the MBG serial bridge pass the bus’s traffic on — a Modbus TCP gateway only answers questions. The line settings are the bus’s, set on the bridge.')}</div>
          </div></div>`;
    },

    _cfMqttHtml(form, test) {
        const { px, M, opts } = form;
        const t = (k, d) => this.t(k, d);
        const edit = opts.context === 'edit';
        return `<fieldset class="cf-fieldset" id="${px}MqttFields"><legend class="sr-only">MQTT</legend>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="${px}Broker">${t('devices.wizard.broker', 'MQTT broker')}</label>
                    <input type="text" id="${px}Broker" class="input" value="${this._esc(M.broker || '')}" placeholder="192.168.1.100" spellcheck="false" autocomplete="off"></div>
                ${this._cfNum(px, 'MqttPort', t('lbl.port', 'Port'), M.mqtt_port || 1883, 1, 65535, '')}
            </div>
            <div class="form-group">
                <label class="form-label" for="${px}MqttTopic">${t('devices.wizard.mqttTopic', 'Topic (subscribe)')}</label>
                ${this._secretInput(`${px}MqttTopic`, M.topic || '', `placeholder="sensors/# or shellies/shelly1/status" aria-describedby="${px}MqttTopicHint ${px}MqttMaskHint"`, t('devices.wizard.mqttTopic', 'Topic (subscribe)'))}
                <div class="field-hint" id="${px}MqttTopicHint">${t('devices.wizard.mqttTopicHint', 'The device subscribes here; values are read from the JSON payload by the template’s json_path. + and # wildcards supported.')}</div>
                <div class="field-hint" id="${px}MqttMaskHint"><i aria-hidden="true" class="bi bi-eye-slash"></i> ${t('cf.maskedWhy', 'The topic and its variables are hidden on screen: they can carry an identifier of the installation (a GX portal id). Show them with the eye button.')}</div>
                ${edit ? '' : `<div class="cf-presets"><span class="field-hint">${t('devices.wizard.presets', 'Presets:')}</span>
                    <button type="button" class="calc-chip" ${this._act('_devWizMqttPreset', ['zigbee2mqtt_sensor', 'zigbee2mqtt/<friendly_name>'])}><i aria-hidden="true" class="bi bi-broadcast-pin"></i> Zigbee (zigbee2mqtt)</button>
                    <button type="button" class="calc-chip" ${this._act('_devWizMqttPreset', ['ble_theengs_sensor', 'home/TheengsGateway/BTtoMQTT/<MAC>'])}><i aria-hidden="true" class="bi bi-bluetooth"></i> BLE (Theengs/BTHome)</button></div>`}
            </div>
            <div class="form-group">
                <label class="form-label" for="${px}MqttVars">${t('devices.mqttVars', 'Topic variables')}</label>
                ${this._secretInput(`${px}MqttVars`, window.ConnformModel.vars2text(M.vars), `placeholder="portal=…, vebus=274" aria-describedby="${px}MqttVarsHint ${px}MqttMaskHint"`, t('devices.mqttVars', 'Topic variables'))}
                <div class="field-hint" id="${px}MqttVarsHint">${t('devices.mqttVarsHint', 'name=value, separated by commas: what {name} stands for in the template’s topics. A value that itself holds “, name=” writes that comma as \\,')}</div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="${px}MqttUser">${t('lbl.username', 'Username')}</label>
                    <input type="text" id="${px}MqttUser" class="input" value="${this._esc(M.username || '')}" autocomplete="off"></div>
                <div class="form-group"><label class="form-label" for="${px}MqttPass">${t('lbl.password', 'Password')}</label>
                    ${this._secretInput(`${px}MqttPass`, M.password || '', edit ? `placeholder="${M.has_password ? '••••••' : ''}" aria-describedby="${px}MqttPassHint"` : '', t('lbl.password', 'Password'))}
                    ${edit ? `<div class="field-hint" id="${px}MqttPassHint">${t('devices.editPassKeep', 'Leave the password blank to keep the stored one.')}</div>` : ''}</div>
                <div class="form-group cf-checks">
                    <label class="checkbox-label"><input type="checkbox" id="${px}MqttTls" ${M.tls ? 'checked' : ''}> ${t('devices.wizard.mqttTls', 'TLS (8883)')}</label>
                    <label class="checkbox-label" title="${this._esc(t('devices.mqttTlsInsecureHint', 'Encrypted, but the certificate is not checked — for a device with its own certificate (a Victron GX).'))}">
                        <input type="checkbox" id="${px}MqttTlsInsecure" ${M.tls_insecure ? 'checked' : ''}> ${t('devices.mqttTlsInsecure', 'Accept its own certificate')}</label>
                </div>
            </div>
            ${this._cfKeepaliveHtml(form)}
            <div class="cf-test">
                <button type="button" data-live class="btn btn-secondary btn-sm" id="${px}TestBtn" data-action="${px === 'devWiz' ? 'devWizardTest' : 'deviceDetailTest'}" data-with-el>
                    <i aria-hidden="true" class="bi bi-activity"></i> ${t('devices.wizard.testConn', 'Test connection')}</button>
                <button type="button" class="btn btn-secondary btn-sm" id="${px}MqttBrowseBtn" ${this._act('_cfMqttBrowse', [px], { el: true })}>
                    <i aria-hidden="true" class="bi bi-binoculars"></i> ${t('devices.wizard.browseTopics', 'Browse topics')}</button>
                <span class="wiz-test-result" id="${px}TestResult" role="status" aria-live="polite"></span>
            </div>
            <div id="${px}MqttBrowse" hidden>
                <input type="text" id="${px}MqttBrowseFilter" class="input" aria-label="${this._esc(t('devices.wizard.browseFilter', 'Filter topics…'))}"
                       placeholder="${this._esc(t('devices.wizard.browseFilter', 'Filter topics…'))}" ${this._act('_cfBrowseRender', [px], { on: 'input' })}>
                <div class="mqtt-browse-list" id="${px}MqttBrowseList" role="listbox" aria-label="${this._esc(t('cf.topicsHeard', 'Topics heard'))}"></div>
                <div class="field-hint" id="${px}MqttBrowseHint" role="status"></div>
            </div></fieldset>`;
    },

    // how an MQTT device is asked to keep publishing: the template's
    // keepalive, its own (topic, payloads, every N s), or none
    _cfKeepaliveHtml(form) {
        const { px, M } = form;
        const t = (k, d) => this.t(k, d);
        const s = window.ConnformModel.display(M);
        const tk = M.template_keepalive;
        const tplText = tk ? `${t('devices.kaTemplate', 'As the template says')} (${tk.every_s ?? 30} s)` : t('devices.kaTemplateNone', 'As the template says (none)');
        return `<div class="form-row">
                <div class="form-group"><label class="form-label" for="${px}KaMode">${t('devices.keepalive', 'Keepalive')}</label>
                    <select id="${px}KaMode" class="input" aria-describedby="${px}KaHint" ${this._act('_cfKaModeChanged', [px], { value: true, on: 'change' })}>
                        <option value="template" ${s.ka_mode === 'template' ? 'selected' : ''}>${this._esc(tplText)}</option>
                        <option value="custom" ${s.ka_mode === 'custom' ? 'selected' : ''}>${t('devices.kaCustom', 'Its own')}</option>
                        <option value="off" ${s.ka_mode === 'off' ? 'selected' : ''}>${t('devices.kaOff', 'Off')}</option></select>
                    <div class="field-hint" id="${px}KaHint">${t('devices.kaHint', 'For a device that publishes only while it is asked (a Victron GX). The first payload goes out on connect, the other every N seconds (5–3600).')}</div></div>
            </div>
            <div class="form-row" id="${px}KaFields" ${s.ka_mode === 'custom' ? '' : 'hidden'}>
                <div class="form-group flex-2"><label class="form-label" for="${px}KaTopic">${t('devtpl.kaTopic', 'Keepalive topic')}</label>
                    ${this._secretInput(`${px}KaTopic`, s.ka_topic, 'placeholder="R/{portal}/keepalive"')}</div>
                <div class="form-group"><label class="form-label" for="${px}KaFirst">${t('devtpl.kaFirst', 'First payload')}</label>
                    <input class="input" id="${px}KaFirst" value="${this._esc(s.ka_first)}"></div>
                <div class="form-group flex-2"><label class="form-label" for="${px}KaPayload">${t('devtpl.kaPayload', 'Then')}</label>
                    <input class="input" id="${px}KaPayload" value="${this._esc(s.ka_payload)}"></div>
                ${this._cfNum(px, 'KaEvery', t('devtpl.kaEvery', 'Every (s)'), s.ka_every, 5, 3600, '')}
            </div>`;
    },

    _cfKaModeChanged(px, mode) {
        const f = document.getElementById(px + 'KaFields');
        if (f) f.hidden = mode !== 'custom';
    },

    // ── MQTT topic browse: collect the broker's speaking topics, click to fill
    async _cfMqttBrowse(px, btn) {
        const g = id => document.getElementById(px + id);
        const form = (this._cf || {})[px];
        if (!form) return;
        form.read();
        const M = form.M;
        const out = g('TestResult');
        if (!M.broker) {
            out.className = 'wiz-test-result err';
            out.textContent = this.t('devices.wizard.browseNeedBroker', 'Enter the broker address first.');
            return;
        }
        btn.disabled = true;
        g('MqttBrowse').hidden = false;
        g('MqttBrowseList').innerHTML = `<div class="field-hint" style="padding:8px;">${this._esc(this.t('devices.wizard.browseListening', 'Listening 3s — retained topics appear instantly, live ones as they publish…'))}</div>`;
        g('MqttBrowseHint').textContent = '';
        try {
            const body = { broker: M.broker, port: M.mqtt_port || 1883, username: M.username || '', password: M.password || '', tls: !!M.tls };
            const r = await fetch('/api/discover/mqtt/browse', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [r.statusText]).join('; '));
            if (!data.ok) throw new Error(data.error || 'browse failed');
            form.topics = data.topics || [];
            this._cfBrowseRender(px);
            g('MqttBrowseHint').textContent = data.truncated
                ? this.t('devices.wizard.browseTruncated', 'Topic list truncated — narrow it with the filter field.')
                : this.t('devices.wizard.browsePick', 'Click a topic to use it as the subscribe topic.');
        } catch (e) {
            g('MqttBrowseList').innerHTML = `<div class="err" style="padding:8px;">${this._esc(String(e.message || e))}</div>`;
        } finally { btn.disabled = false; }
    },

    _cfBrowseRender(px) {
        const form = (this._cf || {})[px];
        const list = document.getElementById(px + 'MqttBrowseList');
        if (!list || !form) return;
        const q = (document.getElementById(px + 'MqttBrowseFilter')?.value || '').toLowerCase();
        const all = form.topics || [];
        const rows = q ? all.filter(x => x.topic.toLowerCase().includes(q)) : all;
        if (!rows.length) {
            list.innerHTML = `<div class="field-hint" style="padding:8px;">${this._esc(all.length
                ? this.t('devices.wizard.browseNoMatch', 'No topic matches the filter.')
                : this.t('devices.wizard.browseEmpty', 'No topics heard — the broker may be idle (nothing retained, nobody publishing). Try again while the device is sending.'))}</div>`;
            return;
        }
        list.innerHTML = rows.slice(0, 400).map(x => `
            <div class="mqtt-browse-row" role="option" tabindex="0" aria-selected="false" ${this._act('_cfBrowsePick', [px, x.topic])} data-key-enter>
                <span class="mqtt-browse-topic">${this._esc(x.topic)}</span>
                ${x.retained ? '<span class="badge badge-secondary">retained</span>' : ''}
                ${x.count > 1 ? `<span class="mqtt-browse-count">×${x.count}</span>` : ''}
                <span class="mqtt-browse-payload">${this._esc((x.payload || '').slice(0, 120))}</span>
            </div>`).join('');
    },

    _cfBrowsePick(px, topic) {
        const inp = document.getElementById(px + 'MqttTopic');
        if (inp) { inp.value = topic; inp.focus(); }
        const hint = document.getElementById(px + 'MqttBrowseHint');
        if (hint) hint.textContent = this.t('devices.wizard.browsePicked', 'Topic set. Wildcards + and # still work if you generalize it.');
    },

    // ── Test: the one connection the form holds, as the server would use it
    async _cfTest(px, btn) {
        const form = (this._cf || {})[px];
        if (!form) return;
        const { model: M, errors } = form.read();
        const out = document.getElementById(px + 'TestResult');
        const orig = btn ? btn.innerHTML : '';
        // what the form can see is said at its field first
        const local = Object.fromEntries(Object.entries(errors).filter(([k]) => !k.startsWith('connection.keepalive')));
        if (Object.keys(local).length) {
            const left = form.showErrors(local);
            if (out) { out.className = 'wiz-test-result err'; out.textContent = '✗ ' + (left.length ? left.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.')); }
            return;
        }
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span> …'; }
        try {
            const id = form.opts.deviceId;
            let r;
            if (id && !form.dirty()) {
                // unchanged: the saved device is asked — its stored secrets,
                // what its running input hears; never a second connection to a
                // broker that allows few
                r = await fetch(`/api/devices/${encodeURIComponent(id)}/test`, { method: 'POST' });
            } else {
                // edited or new: the connection a save would leave — the
                // edited values (on a bridge: the bus picked, resolved by the
                // server), a bridge left said, and what the form does not show
                // (a stored password, headers) taken from the saved device.
                // An edited tap is not probed: the server says save first.
                r = await fetch('/api/devices/test', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ connection: form.testConnection(), ...(id ? { device_id: id } : {}),
                                           template: (form.opts.template && form.opts.template()) || '' }),
                });
            }
            if (out) this._showTestResult(out, r, await r.json().catch(() => ({})));
        } catch (e) {
            if (out) { out.className = 'wiz-test-result err'; out.textContent = '✗ ' + e.message; }
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },
});
