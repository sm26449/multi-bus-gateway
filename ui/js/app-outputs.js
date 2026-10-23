/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// A device's Outputs tab: where its values go. On the left, every output with
// its state and a one-line summary (which is also the answer to "where does
// this device publish?"); on the right, the chosen one's settings.
//
// Each output is described ONCE in _outputDefs(): its switch, its state, its
// summary and its settings. The list, the header, the switch and the
// remembered choice are built from that description, so an output added
// later is one entry, not another card to copy. Every switch applies at once;
// an output with settings to type (REST push, the OPC UA values) saves them
// from its own panel.
Object.assign(JanitzaMonitor.prototype, {

    _outputDefs() {
        const s = this._devDetail, d = s.data, id = s.id;
        const t = (k, f) => this.t(k, f);
        const primary = !!s.primary, unit = !!s.endpoint_id;
        const rp = s.entry?.rest_push || {};
        const installation = t('devices.sink.endpointLevel', 'Set on the installation — applies to every unit');
        const endpointWide = unit ? t('devices.endpointUnit.sinkShared', 'Declared on the installation — switching this applies to every unit of') + ` <code>${this._esc(s.endpoint_id)}</code>.` : '';
        const opcuaFields = d.opcua_fields || [];
        let restHost = '';
        try { restHost = rp.url ? new URL(rp.url).host : ''; } catch (e) { restHost = rp.url || ''; }
        return [
            { key: 'mqtt', icon: 'broadcast', title: 'MQTT',
              on: !!d.mqtt_enabled, sink: s.sinks?.mqtt, summary: d.topic_prefix,
              lock: primary ? t('devices.sink.lockedPrimary', 'Always on for device #1') : unit ? installation : '',
              body: () => this._outMqttBody(d, primary, unit),
              set: on => this._postOutputs(id, { mqtt: on }) },
            { key: 'influxdb', icon: 'database', title: 'InfluxDB',
              on: !!d.influxdb_enabled, sink: s.sinks?.influxdb, summary: d.bucket,
              lock: primary ? t('devices.sink.lockedPrimary', 'Always on for device #1') : unit ? installation : '',
              body: () => this._outInfluxBody(d, primary, unit),
              set: on => this._postOutputs(id, { influxdb: on }) },
            { key: 'http', icon: 'braces', title: t('devices.sink.httpTitle', 'HTTP / JSON output'),
              on: !!d.http_output_enabled, sink: s.sinks?.http, summary: `/api/meters/${id}`, scope: endpointWide,
              body: () => this._outHttpBody(d, id),
              set: on => this._postHttpOutput(id, on),
              shown: () => d.http_output_enabled && this._loadHttpFeed(id) },
            { key: 'opcua', icon: 'hdd-network', title: 'OPC UA',
              on: !!d.opcua_enabled, sink: s.sinks?.opcua,
              summary: opcuaFields.length ? t('devices.sink.opcuaCount', '{n} chosen').replace('{n}', opcuaFields.length)
                                          : t('devices.sink.opcuaAll', 'Every value'),
              scope: unit ? t('devices.sink.opcuaGroup', 'Set for every unit of its group — they share a template, so they share the choice.') : '',
              body: () => this._outOpcuaBody(d),
              set: on => this._postOpcua(id, on, d.opcua_fields || []),
              shown: () => d.opcua_enabled && opcuaFields.length && this._loadOpcuaFields(id) },
            { key: 'rest', icon: 'cloud-upload', title: t('rest.title', 'REST push'),
              on: !!rp.enabled, sink: s.sinks?.rest, summary: restHost, scope: endpointWide,
              body: () => this._outRestBody(rp),
              set: on => this._saveRestPush(id, on) },
        ];
    },

    // ── the frame: list + chosen output ──
    _outputsHtml() {
        const defs = this._outputDefs();
        const cur = defs.find(o => o.key === this._outputKey()) || defs[0];
        const t = (k, f) => this.t(k, f);
        const unit = this._devDetail.endpoint_id;
        return `
        ${unit ? `<div class="field-hint outputs-unit-note">
            <span><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.endpointUnit.outputs', 'Where this unit publishes is set on its installation, per group, and applies to every unit of the group.')}</span>
            <button class="btn btn-secondary btn-sm" ${this._act('openEndpointDetail', [unit])}><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.openInstallation', 'Open installation')}</button>
        </div>` : ''}
        <div class="outputs-layout">
            <div class="outputs-nav" role="tablist" aria-orientation="vertical" aria-label="${t('devices.outputs.where', 'Where the values go')}">
                ${defs.map(o => this._outputNavItem(o, o === cur)).join('')}
            </div>
            <section class="outputs-detail settings-card" role="tabpanel" id="outDetail" aria-labelledby="outTab-${cur.key}">
                ${this._outputDetailHtml(cur)}
            </section>
        </div>`;
    },

    _outputNavItem(o, selected) {
        const st = this._sinkState(o.sink, o.on, this._devDetail?.entry?.connected);
        return `
        <button type="button" role="tab" class="outputs-nav-item${selected ? ' active' : ''}" id="outTab-${o.key}"
                aria-selected="${selected}" aria-controls="outDetail" tabindex="${selected ? 0 : -1}"
                ${this._act('selectOutput', [o.key])}>
            <i aria-hidden="true" class="bi bi-${o.icon}"></i>
            <span class="o-main">
                <span class="o-title">${this._esc(o.title)}</span>
                ${o.on && o.summary ? `<span class="o-summary">${this._esc(o.summary)}</span>` : ''}
            </span>
            <span class="o-state ${st.cls}"><span class="o-dot" aria-hidden="true"></span>${this._esc(st.label)}</span>
        </button>`;
    },

    _outputDetailHtml(o) {
        const t = (k, f) => this.t(k, f);
        return `
        <div class="settings-card-header">
            <h3><i aria-hidden="true" class="bi bi-${o.icon}"></i> ${this._esc(o.title)} ${this._sinkStatusPill(o.sink, o.on, this._devDetail?.entry?.connected)}</h3>
            <label class="switch-label" ${o.lock ? `title="${this._esc(o.lock)}"` : ''}>
                <input data-admin type="checkbox" id="outEnabled" ${o.on ? 'checked' : ''} ${o.lock ? 'disabled' : ''}
                       ${this._act('toggleOutput', [o.key], {el: true, on: 'change'})}>
                <span>${t('devices.sink.enable', 'Enable')}</span>
            </label>
        </div>
        <div class="settings-card-body">
            ${o.body()}
            ${o.lock ? `<p class="field-hint"><i aria-hidden="true" class="bi bi-lock"></i> ${this._esc(o.lock)}</p>` : ''}
            ${o.scope ? `<p class="field-hint"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${o.scope}</p>` : ''}
        </div>`;
    },

    // which output is open: the last one chosen (any device), else the first
    _outputKey() {
        if (this._outputSel) return this._outputSel;
        try { return localStorage.getItem('mbg-output') || ''; } catch (e) { return ''; }
    },

    selectOutput(key) {
        this._outputSel = key;
        try { localStorage.setItem('mbg-output', key); } catch (e) { /* remembered for this page only */ }
        this._renderOutputs(true);
    },

    _renderOutputs(focus = false) {
        const host = document.getElementById('outputsHost');
        if (!host || !this._devDetail) return;
        host.innerHTML = this._outputsHtml();
        const nav = host.querySelector('.outputs-nav');
        if (nav) nav.addEventListener('keydown', ev => this._outputsKey(ev, nav));
        const active = host.querySelector('.outputs-nav-item.active');
        // a phone shows the list as a row: bring the open one into view, sideways only
        if (nav && active && nav.scrollWidth > nav.clientWidth) {
            nav.scrollLeft = Math.max(0, active.offsetLeft - nav.offsetLeft - 8);
        }
        if (focus) active?.focus({ preventScroll: true });
        this._outputShown();
    },

    // the arrows move along the list (either way: it is a row on a phone)
    _outputsKey(ev, nav) {
        const items = [...nav.querySelectorAll('.outputs-nav-item')];
        const i = items.indexOf(document.activeElement);
        if (i < 0) return;
        const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[ev.key];
        const to = ev.key === 'Home' ? 0 : ev.key === 'End' ? items.length - 1
                 : step ? (i + step + items.length) % items.length : null;
        if (to === null) return;
        ev.preventDefault();
        this.selectOutput(items[to].id.replace('outTab-', ''));
    },

    // what an output loads when it is shown (the JSON feed, the chosen values)
    _outputShown() {
        const cur = this._outputDefs().find(o => o.key === this._outputKey()) || this._outputDefs()[0];
        if (cur.shown) cur.shown();
    },

    async toggleOutput(key, el) {
        const o = this._outputDefs().find(x => x.key === key);
        if (!o) return;
        const on = !!el.checked;
        el.disabled = true;
        try {
            await o.set(on);
            this.showToast('success', o.title, on ? this.t('devices.sink.nowOn', 'On') : this.t('devices.sink.nowOff', 'Off'));
            this._fetchDevices(true);                       // the device list's pills
        } catch (e) {
            el.checked = !on;
            this.showToast('error', o.title, e.message);
        } finally {
            el.disabled = false;
            this._renderOutputs();
        }
    },

    async _postJson(url, body) {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(body) });
        const res = await r.json().catch(() => ({}));
        if (!r.ok) {
            const errs = res.detail?.errors || [res.detail || 'failed'];
            throw new Error(Array.isArray(errs) ? errs.join(' · ') : String(errs));
        }
        return res;
    },

    // state shared by the list, the header pill and the device overview
    _sinkState(sink, enabled, deviceLive) {
        const t = (k, f) => this.t(k, f);
        if (!enabled) return { cls: 'off', label: t('devices.sink.off', 'off here'),
            tip: t('devices.sink.offTip', 'Turned off for this device (its Outputs tab).') };
        if (!sink?.available) return { cls: 'off', label: t('devices.sink.noService', 'off in Settings'),
            tip: t('devices.sink.noServiceTip', 'The service is off for the whole gateway (Settings).') };
        if (!sink.connected) return { cls: 'warn', label: t('devices.sink.down', 'not connected') };
        if (deviceLive === false) return { cls: 'warn', label: t('devices.sink.idle', 'idle · no data') };
        return { cls: 'ok', label: t('devices.sink.active', 'active') };
    },

    _sinkStatusPill(sink, enabled, deviceLive) {
        const st = this._sinkState(sink, enabled, deviceLive);
        return `<span class="sink-pill ${st.cls}"${st.tip ? ` title="${this._esc(st.tip)}"` : ''}>${this._esc(st.label)}</span>`;
    },

    _patchSink(name, patch) {
        const s = this._devDetail;
        if (!s) return;
        s.sinks = s.sinks || {};
        s.sinks[name] = { ...(s.sinks[name] || {}), ...patch };
    },

    // ── MQTT and InfluxDB ──
    async _postOutputs(id, flags) {
        const res = await this._postJson(`/api/devices/${encodeURIComponent(id)}/outputs`, flags);
        const d = this._devDetail?.data;
        if (d) {
            d.mqtt_enabled = res.mqtt_enabled;
            d.influxdb_enabled = res.influxdb_enabled;
            d.ha_discovery_enabled = res.ha_discovery_enabled;
        }
        this._patchSink('mqtt', { enabled: res.mqtt_enabled });
        this._patchSink('influxdb', { enabled: res.influxdb_enabled });
        return res;
    },

    async toggleHaDiscovery(id, el) {
        const on = !!el.checked;
        el.disabled = true;
        try {
            await this._postOutputs(id, { ha_discovery: on });
            this.showToast('success', 'Home Assistant', on ? this.t('devices.sink.haOn', 'Discovery published')
                                                           : this.t('devices.sink.haOff', 'Discovery no longer published'));
        } catch (e) {
            el.checked = !on;
            this.showToast('error', 'Home Assistant', e.message);
        } finally { el.disabled = false; }
    },

    _outMqttBody(d, primary, unit) {
        const t = (k, f) => this.t(k, f);
        return `
        <div class="form-group"><label class="form-label" for="ddvTopic">${t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
            <input type="text" id="ddvTopic" class="input" value="${this._esc(d.topic_prefix)}" disabled></div>
        <label style="display:flex;align-items:center;gap:8px;margin-top:6px;">
            <input data-admin type="checkbox" id="ddvHaDisc" ${d.ha_discovery_enabled ? 'checked' : ''} ${unit || primary ? 'disabled' : ''}
                   ${this._act('toggleHaDiscovery', [this._devDetail.id], {el: true, on: 'change'})}>
            ${t('devices.wizard.haDiscovery', 'Publish Home Assistant MQTT discovery for this device')}</label>
        ${unit ? '' : `<p class="field-hint"><i aria-hidden="true" class="bi bi-lock"></i> ${primary
            ? t('devices.sink.mqttLockNote', 'Topic and MQTT output are fixed for device #1 (protects existing history & Home Assistant).')
            : t('devices.sink.routeLockNote', 'Topic prefix is fixed after creation — changing it would orphan existing history & HA entities.')}</p>`}
        ${this._outPublishingHtml('mqtt')}`;
    },

    _outInfluxBody(d, primary, unit) {
        const t = (k, f) => this.t(k, f);
        return `
        <div class="form-row">
            <div class="form-group"><label class="form-label" for="ddvBucket">${t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                <input type="text" id="ddvBucket" class="input" value="${this._esc(d.bucket)}" disabled></div>
            <div class="form-group"><label class="form-label" for="ddvTag">${t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                <input type="text" id="ddvTag" class="input" value="${this._esc(d.device_tag)}" disabled></div>
        </div>
        ${unit ? '' : `<p class="field-hint"><i aria-hidden="true" class="bi bi-lock"></i> ${primary
            ? t('devices.sink.influxLockNote', 'Bucket and tag are fixed for device #1 (protects existing history).')
            : t('devices.sink.influxLockNoteEdit', 'Bucket and tag are fixed after creation — changing them would orphan existing history.')}</p>`}
        ${this._outPublishingHtml('influxdb')}`;
    },

    // ── publishing: when a value goes out (on change or every reading, how
    //    often an unchanged one is resent / at most how often a point is
    //    written) — the device's own, or the gateway's (Settings) ──
    _pubWords(sect, mode, secs) {
        const t = (k, f) => this.t(k, f);
        const n = v => String(Number(v));
        if (sect === 'mqtt') {
            if (mode === 'all') return t('devices.pub.mqttAll', 'every reading');
            return Number(secs) > 0
                ? t('devices.pub.mqttChanged', 'on change · an unchanged value resent every {s} s').replace('{s}', n(secs))
                : t('devices.pub.mqttChangedOnly', 'on change only');
        }
        return (mode === 'all' ? t('devices.pub.influxAll', 'every reading') : t('devices.pub.influxChanged', 'on change'))
            + ' · ' + t('devices.pub.influxAtMost', 'at most one point every {s} s').replace('{s}', n(secs));
    },

    _outPublishingHtml(sect) {
        const p = this._devDetail.entry?.publishing;
        if (!p) return '';
        const t = (k, f) => this.t(k, f);
        const key = sect === 'mqtt' ? 'heartbeat_interval' : 'write_interval';
        const own = p.own[sect], set = p.settings[sect], eff = p.effective[sect];
        const settingsLink = `<a href="#" ${this._act('openSettingsTab', [sect])}>${t('devices.pub.settingsLink', 'Settings')} → ${sect === 'mqtt' ? 'MQTT' : 'InfluxDB'}</a>`;
        const head = `<h4 class="outputs-sub">${t('devices.pub.title', 'Publishing')}</h4>`;
        if (!p.editable) {
            return `${head}<p class="field-hint">${this._esc(this._pubWords(sect, eff.publish_mode, eff[key]))} — ${t('devices.pub.primary', 'the gateway\'s own device follows')} ${settingsLink}.</p>`;
        }
        const asSettings = t('devices.pub.asSettings', 'As in Settings') + ` (${this._pubWords(sect, set.publish_mode, set[key])})`;
        const opt = (v, label) => `<option value="${v}" ${(own.publish_mode || '') === v ? 'selected' : ''}>${this._esc(label)}</option>`;
        const unit = this._devDetail.endpoint_id;
        return `
        ${head}
        <div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="pubMode-${sect}">${t('devices.pub.when', 'Send a value')}</label>
                <select id="pubMode-${sect}" class="input" data-admin>
                    ${opt('', asSettings)}${opt('changed', t('devices.pub.onChange', 'On change'))}${opt('all', t('devices.pub.every', 'At every reading'))}
                </select></div>
            <div class="form-group"><label class="form-label" for="pubSecs-${sect}">${sect === 'mqtt'
                    ? t('devices.pub.heartbeat', 'Resend an unchanged value every (s)')
                    : t('devices.pub.interval', 'At most one point every (s)')}</label>
                <input type="number" id="pubSecs-${sect}" class="input" data-admin min="0" max="86400" step="1"
                       value="${own[key] ?? ''}" placeholder="${this._esc(String(set[key]))}"></div>
        </div>
        <p class="field-hint">${t('devices.pub.blank', 'Blank = as in')} ${settingsLink}${sect === 'mqtt' ? ` · ${t('devices.pub.zero', '0 = never resend')}` : ''}.
            ${t('devices.pub.now', 'Now')}: <strong id="pubNow-${sect}">${this._esc(this._pubWords(sect, eff.publish_mode, eff[key]))}</strong>.</p>
        ${unit ? `<p class="field-hint"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.pub.group', 'Set for every unit of its group.')}</p>` : ''}
        <div class="outputs-actions">
            <button data-admin type="button" class="btn btn-primary btn-sm" ${this._act('savePublishing', [sect])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('devices.pub.save', 'Save publishing')}</button>
        </div>`;
    },

    async savePublishing(sect) {
        const id = this._devDetail?.id;
        const key = sect === 'mqtt' ? 'heartbeat_interval' : 'write_interval';
        const mode = document.getElementById(`pubMode-${sect}`)?.value || null;
        const raw = (document.getElementById(`pubSecs-${sect}`)?.value || '').trim();
        const secs = raw === '' ? null : Number(raw);
        if (secs !== null && !(secs >= 0 && secs <= 86400)) {
            this.showToast('error', this.t('devices.pub.title', 'Publishing'), this.t('devices.pub.badSecs', 'Seconds, 0 to 86400 — or blank for Settings.'));
            return;
        }
        try {
            const res = await this._postJson(`/api/devices/${encodeURIComponent(id)}/publishing`,
                                             { [sect]: { publish_mode: mode, [key]: secs } });
            if (this._devDetail?.entry) this._devDetail.entry.publishing = res.publishing;
            const eff = res.publishing.effective[sect];
            this.showToast('success', this.t('devices.pub.title', 'Publishing'), this._pubWords(sect, eff.publish_mode, eff[key]));
            this._renderOutputs();
        } catch (e) {
            this.showToast('error', this.t('devices.pub.title', 'Publishing'), e.message);
        }
    },

    openSettingsTab(tab) {
        this.navigateTo('config');
        this.switchConfigTab(tab);
    },

    // ── HTTP / JSON ──
    async _postHttpOutput(id, enabled) {
        await this._postJson(`/api/devices/${encodeURIComponent(id)}/http-output`, { enabled });
        if (this._devDetail?.data) this._devDetail.data.http_output_enabled = enabled;
        this._patchSink('http', { enabled, active: enabled });
    },

    _outHttpBody(d, id) {
        const t = (k, f) => this.t(k, f);
        const url = `${location.origin}/api/meters/${id}`;
        if (!d.http_output_enabled) {
            return `<p class="field-hint">${t('devices.sink.httpNote', 'Serves this device\'s live values as read-only JSON, keyed by measurement name (Solar-API style) — pull it from any HTTP client without MQTT or InfluxDB. Guarded by the same IP allowlist / auth as the UI.')}</p>`;
        }
        return `
        <div class="form-group">
            <label class="form-label" for="ddvHttpUrl">${t('devices.sink.httpUrl', 'JSON feed URL')}</label>
            <div style="display:flex;gap:6px;align-items:center;">
                <input type="text" id="ddvHttpUrl" class="input" value="${this._esc(url)}" readonly data-action="_selectEl" data-with-el>
                <button type="button" class="btn btn-sm btn-secondary" ${this._act('copyText', [url], {el: true})} title="${t('common.copy', 'Copy')}" aria-label="${t('common.copy', 'Copy')}"><i aria-hidden="true" class="bi bi-clipboard"></i></button>
                <a class="btn btn-sm btn-secondary" href="${this._esc(url)}" target="_blank" rel="noopener"><i aria-hidden="true" class="bi bi-box-arrow-up-right"></i> ${t('common.open', 'Open')}</a>
            </div>
        </div>
        <div id="ddvHttpFeed" class="http-feed">
            <div class="http-feed-head">
                <span id="ddvHttpStats" class="http-feed-stats">${t('common.loading', 'Loading…')}</span>
                <button type="button" class="btn btn-sm btn-secondary" ${this._act('_loadHttpFeed', [id])}><i aria-hidden="true" class="bi bi-arrow-clockwise"></i> ${t('common.refresh', 'Refresh')}</button>
            </div>
            <pre id="ddvHttpPreview" class="http-feed-preview" aria-live="polite"></pre>
        </div>`;
    },

    // a live snapshot of what the JSON feed serves (count · freshness · a preview)
    async _loadHttpFeed(id) {
        const stats = document.getElementById('ddvHttpStats');
        const pre = document.getElementById('ddvHttpPreview');
        if (!stats && !pre) return;
        if (stats) stats.textContent = this.t('common.loading', 'Loading…');
        try {
            const r = await fetch(`/api/meters/${encodeURIComponent(id)}`);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const d = await r.json();
            const n = Object.keys(d.values || {}).length;
            const fresh = !d.stale;
            const badge = `<span class="sink-pill ${fresh ? 'ok' : 'warn'}">${fresh
                ? this.t('devices.sink.freshBadge', 'fresh') : this.t('devices.sink.staleBadge', 'stale')}</span>`;
            if (stats) stats.innerHTML =
                `<strong>${n}</strong> ${this.t('devices.sink.httpValues', 'values')} · `
                + `${this.t('devices.sink.updated', 'updated')} ${this._esc(this._fmtFeedTime(d.ts))} ${badge}`;
            if (pre) pre.textContent = JSON.stringify(d, null, 2);
        } catch (e) {
            if (stats) stats.textContent = this.t('devices.sink.httpFetchErr', 'Could not load feed') + ': ' + e.message;
            if (pre) pre.textContent = '';
        }
    },

    _fmtFeedTime(ts) { return this._gatewayTime(ts); },

    // ── OPC UA ──
    _outOpcuaBody(d) {
        const s = this._devDetail.sinks?.opcua || {};
        const id = this._devDetail.id;
        const t = (k, f) => this.t(k, f);
        const chosen = (d.opcua_fields || []).length > 0;
        const server = s.available
            ? `<code>${this._esc((s.endpoint || '').replace('0.0.0.0', location.hostname))}</code>`
            : t('devices.sink.opcuaServerOff', 'The OPC UA server is off — Settings → OPC UA.');
        return `
        <p class="field-hint">${this._esc(t('devices.sink.opcuaNote', 'Serves this device on the gateway\'s OPC UA server, for SCADA systems and HMI panels: each value with its quality and read time, under Devices/<device>/<value>.'))} ${server}</p>
        ${d.opcua_enabled ? `
        <fieldset class="outputs-choice">
            <legend class="form-label">${t('devices.sink.opcuaWhich', 'Values served')}</legend>
            <label><input data-admin type="radio" name="ddvOpcuaMode" value="all" ${chosen ? '' : 'checked'} ${this._act('_opcuaMode', [id], {el: true, on: 'change'})}> ${t('devices.sink.opcuaAll', 'Every value')}</label>
            <label><input data-admin type="radio" name="ddvOpcuaMode" value="chosen" ${chosen ? 'checked' : ''} ${this._act('_opcuaMode', [id], {el: true, on: 'change'})}> ${t('devices.sink.opcuaChosen', 'Only the values I choose')}</label>
        </fieldset>
        <div id="ddvOpcuaPick" ${chosen ? '' : 'hidden'}>
            <input type="search" id="ddvOpcuaFilter" class="input" placeholder="${t('common.filter', 'Filter')}" aria-label="${t('common.filter', 'Filter')}" ${this._act('_opcuaFilter', [], {el: true, on: 'input'})}>
            <div id="ddvOpcuaList" class="outputs-pick-list">${t('common.loading', 'Loading…')}</div>
            <div class="outputs-actions">
                <span id="ddvOpcuaCount" class="field-hint"></span>
                <button data-admin type="button" class="btn btn-primary btn-sm" ${this._act('saveOpcuaFields', [id])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('devices.sink.opcuaSave', 'Serve these values')}</button>
            </div>
        </div>` : ''}`;
    },

    async _postOpcua(id, enabled, fields) {
        const res = await this._postJson(`/api/devices/${encodeURIComponent(id)}/opcua-output`, { enabled, fields });
        const d = this._devDetail?.data;
        if (d) {
            d.opcua_enabled = res.opcua_enabled;
            d.opcua_fields = res.opcua_fields || [];
        }
        const s = this._devDetail?.sinks?.opcua || {};
        this._patchSink('opcua', { enabled: res.opcua_enabled, fields: res.opcua_fields || [],
                                   active: res.opcua_enabled && !!s.connected });
        return res;
    },

    async _opcuaMode(id, el) {
        const chosen = el.value === 'chosen';
        const pick = document.getElementById('ddvOpcuaPick');
        if (pick) pick.hidden = !chosen;
        if (chosen) { this._loadOpcuaFields(id); return; }
        try {                                            // back to every value: applies at once
            await this._postOpcua(id, true, []);
            this.showToast('success', 'OPC UA', this.t('devices.sink.opcuaAllOn', 'Every value is served'));
            this._renderOutputs();
        } catch (e) {
            this.showToast('error', this.t('common.error', 'Error'), e.message);
        }
    },

    async _loadOpcuaFields(id) {
        const list = document.getElementById('ddvOpcuaList');
        if (!list) return;
        let rows = [];
        try {
            rows = (await (await fetch(`/api/registers/selected?device=${encodeURIComponent(id)}`)).json()).registers || [];
        } catch (e) { /* the list stays empty, said below */ }
        const picked = new Set(this._devDetail?.data?.opcua_fields || []);
        const seen = new Set();
        rows = rows.filter(r => r.name && !seen.has(r.name) && seen.add(r.name));
        // a chosen value the device no longer declares stays visible, so it can be unticked
        for (const n of picked) if (!seen.has(n)) rows.push({ name: n, label: '' });
        list.innerHTML = rows.length ? rows.map(r => `
            <label class="opcua-pick" data-name="${this._esc((r.name + ' ' + (r.label || '')).toLowerCase())}">
                <input data-admin type="checkbox" value="${this._esc(r.name)}" ${picked.has(r.name) ? 'checked' : ''} ${this._act('_opcuaCount', [], {on: 'change'})}>
                <span>${this._esc(r.label || r.name)}</span> <code class="field-hint">${this._esc(r.name)}</code>${r.unit ? ` <span class="field-hint">${this._esc(r.unit)}</span>` : ''}
            </label>`).join('')
            : `<p class="field-hint">${this.t('devices.sink.opcuaNoValues', 'This device declares no values yet — choose its registers first (Measurements).')}</p>`;
        this._opcuaCount();
    },

    _opcuaFilter(el) {
        const q = (el.value || '').trim().toLowerCase();
        document.querySelectorAll('#ddvOpcuaList .opcua-pick').forEach(row => {
            row.hidden = !!q && !row.dataset.name.includes(q);
        });
    },

    _opcuaCount() {
        const n = document.querySelectorAll('#ddvOpcuaList input[type=checkbox]:checked').length;
        const c = document.getElementById('ddvOpcuaCount');
        if (c) c.textContent = this.t('devices.sink.opcuaCount', '{n} chosen').replace('{n}', n);
    },

    async saveOpcuaFields(id) {
        const fields = [...document.querySelectorAll('#ddvOpcuaList input[type=checkbox]:checked')].map(x => x.value);
        if (!fields.length) {
            this.showToast('error', 'OPC UA', this.t('devices.sink.opcuaNone', 'Choose at least one value, or serve every value.'));
            return;
        }
        try {
            await this._postOpcua(id, true, fields);
            this.showToast('success', 'OPC UA', this.tn('devices.sink.opcuaSaved', fields.length, '{n} values served'));
            this._renderOutputs();
        } catch (e) {
            this.showToast('error', this.t('common.error', 'Error'), e.message);
        }
    },

    // ── REST push ──
    _outRestBody(rp) {
        const t = (k, f) => this.t(k, f);
        const id = this._devDetail.id;
        const last = rp.last || {};
        const hdrText = Object.entries(rp.headers || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
        const statusLine = last.ts
            ? (last.ok ? `✓ ${t('rest.ok', 'push OK')} ${last.code ? '(' + last.code + ')' : ''}` : `✕ ${last.error || 'failed'}`)
            : '';
        return `
        <div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="ddvRestUrl">${t('rest.url', 'POST URL')}</label>
                <input type="text" id="ddvRestUrl" class="input" value="${this._esc(rp.url || '')}" placeholder="https://my-cloud/api/telemetry"></div>
            <div class="form-group" style="max-width:110px"><label class="form-label" for="ddvRestInterval">${t('rest.interval', 'Interval (s)')}</label>
                <input type="number" id="ddvRestInterval" class="input" min="5" value="${rp.interval_s || 30}"></div>
            <div class="form-group" style="max-width:120px"><label class="form-label" for="ddvRestFormat">${t('rest.format', 'Format')}</label>
                <select id="ddvRestFormat" class="input"><option value="native" ${rp.format !== 'flat' ? 'selected' : ''}>native</option><option value="flat" ${rp.format === 'flat' ? 'selected' : ''}>flat</option></select></div>
        </div>
        <div class="form-group"><label class="form-label" for="ddvRestHeaders">${t('rest.headers', 'Headers (Name: Value per line)')}</label>
            <textarea id="ddvRestHeaders" class="input" rows="2" spellcheck="false" placeholder="Authorization: Bearer …">${this._esc(hdrText)}</textarea></div>
        <label style="display:flex;align-items:center;gap:8px;margin-top:6px;">
            <input type="checkbox" id="ddvRestVerify" ${rp.verify_tls !== false ? 'checked' : ''}> ${t('rest.verifyTls', 'Verify TLS certificate')}</label>
        <div class="rest-status" id="ddvRestStatus" aria-live="polite">${this._esc(statusLine)}</div>
        <div class="outputs-actions">
            <button data-live class="btn btn-ghost btn-sm" ${this._act('testRestPush', [id])}><i aria-hidden="true" class="bi bi-send"></i> ${t('rest.test', 'Test push')}</button>
            <button data-admin class="btn btn-primary btn-sm" ${this._act('saveRestPush', [id])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
        </div>
        <p class="field-hint"><i aria-hidden="true" class="bi bi-info-circle"></i> ${t('rest.note', 'POSTs the live values of this device as JSON to the URL every interval. External URLs are allowed. Put a Bearer / API key in Headers (stored masked).')}</p>`;
    },

    // the form as it stands; `enabled` from the switch (the model), never a second box
    _restPushPayload(enabled) {
        const g = id => document.getElementById(id);
        const headers = {};
        (g('ddvRestHeaders')?.value || '').split('\n').forEach(line => {
            const i = line.indexOf(':');
            const k = i > 0 ? line.slice(0, i).trim() : '';
            if (k) headers[k] = line.slice(i + 1).trim();
        });
        const rp = this._devDetail?.entry?.rest_push || {};
        return {
            enabled: enabled ?? !!rp.enabled,
            url: (g('ddvRestUrl')?.value ?? rp.url ?? '').trim(),
            interval_s: parseInt(g('ddvRestInterval')?.value, 10) || rp.interval_s || 30,
            format: g('ddvRestFormat')?.value || rp.format || 'native',
            verify_tls: g('ddvRestVerify') ? g('ddvRestVerify').checked : rp.verify_tls !== false,
            headers,
        };
    },

    async _saveRestPush(id, enabled) {
        const res = await this._postJson(`/api/devices/${encodeURIComponent(id)}/rest-push`, this._restPushPayload(enabled));
        if (this._devDetail?.entry) this._devDetail.entry.rest_push = res.rest_push;
        this._patchSink('rest', { enabled: !!res.rest_push?.enabled, active: !!res.rest_push?.enabled });
        return res;
    },

    async saveRestPush(id) {
        try {
            await this._saveRestPush(id);
            this.showToast('success', this.t('rest.title', 'REST push'), this.t('settings.saved', 'Saved & applied'));
            this._fetchDevices(true);
            this._renderOutputs();
            return true;
        } catch (e) {
            this.showToast('error', this.t('common.error', 'Error'), e.message);
            return false;
        }
    },

    async testRestPush(id) {
        if (!(await this.saveRestPush(id))) return;   // the test uses the saved settings
        const box = document.getElementById('ddvRestStatus');
        try {
            const d = await (await fetch(`/api/devices/${encodeURIComponent(id)}/rest-push/test`, { method: 'POST' })).json();
            if (box) box.textContent = d.ok ? `✓ ${this.t('rest.ok', 'push OK')} ${d.code ? '(' + d.code + ')' : ''}` : `✕ ${d.error || 'failed'}`;
            this.showToast(d.ok ? 'success' : 'error', this.t('rest.title', 'REST push'),
                d.ok ? this.t('rest.ok', 'push OK') : (d.error || 'failed'));
        } catch (e) { this.showToast('error', this.t('common.error', 'Error'), e.message); }
    },
});
