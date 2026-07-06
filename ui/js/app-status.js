// status domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ── Status page ───────────────────────────────────────────────────────
    _stopStatusPoll() {
        if (this._statusTimer) { clearInterval(this._statusTimer); this._statusTimer = null; }
    },

    // "12 fails · timeout 8 · exc 02 ILLEGAL DATA ADDRESS: 3 · conn 1" — the
    // taxonomy tells WHICH layer is sick (wiring/slave-id vs map vs network)
    _errBreakdown(counts) {
        if (!counts || !Object.keys(counts).length) return '';
        const EXC = { 1: 'ILLEGAL FUNCTION', 2: 'ILLEGAL DATA ADDRESS', 3: 'ILLEGAL DATA VALUE',
                      4: 'DEVICE FAILURE', 6: 'DEVICE BUSY', 10: 'GW PATH', 11: 'GW TARGET' };
        const parts = Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, n]) => {
            const m = k.match(/^exception_(\d+)$/);
            if (m) {
                const code = Number(m[1]);
                return `<span title="${this._esc(EXC[code] || '')}">exc ${String(code).padStart(2, '0')}: ${n}</span>`;
            }
            const short = { timeout: 'timeout', connection: 'conn', other: 'other' }[k] || k;
            return `${this._esc(short)} ${n}`;
        });
        return ` <span style="color:var(--text-tertiary);font-size:11px;">· ${parts.join(' · ')}</span>`;
    },

    initStatusPage() {
        this._statusHist = this._statusHist || { poll: [], mqtt: [], influx: [] };
        this._statusPrev = null;
        this.renderStatus();
        this._stopStatusPoll();
        this._statusTimer = setInterval(() => {
            if (this.currentPage === 'status' && !document.hidden) this.renderStatus();
        }, 2000);
    },

    async renderStatus() {
        const host = document.getElementById('statusContent');
        if (!host) return;
        let st, vm, res, evLog, al;
        try {
            [st, vm, res, evLog, al] = await Promise.all([
                fetch('/api/status').then(r => r.json()),
                fetch('/api/virtual-meters').then(r => r.json()).catch(() => ({ instances: [] })),
                fetch('/api/status/resources').then(r => r.ok ? r.json() : null).catch(() => null),
                fetch('/api/events?limit=40').then(r => r.ok ? r.json() : null).catch(() => null),
                fetch('/api/alerts?limit=1').then(r => r.ok ? r.json() : null).catch(() => null),
            ]);
        } catch (e) { host.innerHTML = `<p style="color:#c0392b;">${this.t('msg.loadStatus', "Could not load status.")}</p>`; return; }

        const t = this.t.bind(this), esc = s => this._esc(s);
        const devices = st.devices || [], mqtt = st.mqtt || {}, influx = st.influxdb || {}, modbus = st.modbus || {};
        const insts = (vm && vm.instances) || [];
        const OK = '#22c55e', WARN = '#f59e0b', BAD = '#ef4444', OFF = '#9aa4af';

        // rolling rates for sparklines
        const now = Date.now() / 1000, prev = this._statusPrev;
        let mqttRate = null, influxRate = null;
        if (prev && now > prev.t) {
            const dt = now - prev.t;
            mqttRate = Math.max(0, ((mqtt.messages_published || 0) - prev.mqtt) / dt);
            influxRate = Math.max(0, ((influx.writes_total || 0) - prev.influx) / dt);
        }
        this._statusPrev = { t: now, mqtt: mqtt.messages_published || 0, influx: influx.writes_total || 0 };
        const pollRate = devices.reduce((a, d) => a + (d.poll_rate || 0), 0) || (modbus.poll_rate || 0);
        const push = (a, v) => { if (v != null && isFinite(v)) { a.push(Math.round(v * 100) / 100); if (a.length > 60) a.shift(); } };
        push(this._statusHist.poll, pollRate); push(this._statusHist.mqtt, mqttRate); push(this._statusHist.influx, influxRate);

        const dot = c => `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${c};vertical-align:middle;"></span>`;
        const kb = n => n == null ? '—' : (n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B');
        const upt = s => { if (s == null) return '—'; s = Math.floor(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`; };
        const hColor = { ok: OK, degraded: WARN, stale: WARN, down: BAD, idle: OFF };
        const card = (label, value, sub, color) => `<div class="settings-card" style="padding:12px 14px;flex:1;min-width:120px;">
            <div style="color:var(--text-secondary);font-size:11.5px;">${label}</div>
            <div style="font-size:20px;font-weight:600;color:${color || 'inherit'};font-variant-numeric:tabular-nums;">${value}</div>
            ${sub ? `<div style="color:var(--text-secondary);font-size:11px;margin-top:2px;">${sub}</div>` : ''}</div>`;
        const sectHead = (icon, title) => `<h3 style="margin:18px 0 8px;font-size:14px;"><i class="bi ${icon}"></i> ${title}</h3>`;

        // ── health ──
        const issues = [];
        devices.forEach(d => { const h = d.data_health; if (h === 'down') issues.push(`${esc(d.name || d.id)} ${t('status.down', 'down')}`); else if (h === 'degraded' || h === 'stale') issues.push(`${esc(d.name || d.id)} ${h}`); });
        if (mqtt.enabled && !mqtt.connected) issues.push('MQTT ' + t('status.disconnected', 'disconnected'));
        if (influx.enabled && !influx.connected) issues.push('InfluxDB ' + t('status.disconnected', 'disconnected'));
        insts.filter(i => i.enabled && !i.running).forEach(i => issues.push(`vMeter ${esc(i.name || i.template)} ${t('status.stopped', 'stopped')}`));
        const ok = issues.length === 0;
        const banner = `<div class="settings-card" style="padding:14px 16px;display:flex;align-items:center;gap:12px;border-left:4px solid ${ok ? OK : BAD};">
            ${dot(ok ? OK : BAD)}
            <div style="flex:1;">
                <div style="font-weight:600;font-size:15px;">${ok ? t('status.allOk', 'All systems operational') : `${issues.length} ${t('status.issues', 'issue(s)')}`}</div>
                ${ok ? '' : `<div style="color:var(--danger-text,#c0392b);font-size:12.5px;margin-top:2px;">${issues.map(esc).join(' · ')}</div>`}
            </div>
            <div style="text-align:right;color:var(--text-secondary);font-size:12px;">
                <div>v${esc(st.version || '')} · ${t('status.uptime', 'uptime')} ${upt(res && res.uptime_s)}</div>
                <div style="margin-top:3px;">${(() => {
                    const a = al && al.status;
                    if (!a) return '';
                    const on = a.enabled, chans = (a.channels || []).join(', ') || t('status.noChannel', 'no channel');
                    const testBtn = (a.channels && a.channels.length)
                        ? ` <button class="btn btn-ghost btn-sm" style="padding:0 8px;font-size:11px;" onclick="app.testAlert(this)"><i class="bi bi-send"></i> ${t('status.testAlert', 'Test')}</button> <span id="alertTestResult" style="font-size:11.5px;"></span>`
                        : '';
                    return `<i class="bi bi-bell${on ? '-fill' : ''}"></i> ${t('status.alerts', 'Alerts')}: <b style="color:${on ? OK : OFF};">${on ? t('status.armed', 'armed') : t('status.off', 'off')}</b>${on ? ` <span style="color:var(--text-secondary);">· ${esc(chans)}</span>` : ''}${testBtn}`;
                })()}</div>
            </div></div>`;

        // ── data pipeline ──
        const srcRows = devices.map(d => `<div style="display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12.5px;">
            ${dot(hColor[d.data_health] || OFF)}<b>${esc(d.name || d.id)}</b>
            <span style="color:var(--text-secondary);margin-left:auto;font-variant-numeric:tabular-nums;">${(d.poll_rate ?? 0).toFixed ? (d.poll_rate ?? 0).toFixed(1) : d.poll_rate}/s · ${d.staleness_age_s != null ? d.staleness_age_s + 's' : '—'}</span></div>`).join('') || `<span class="field-hint">${t('status.noDevices', 'no devices')}</span>`;
        const vmReq = insts.reduce((a, i) => a + (i.req_rate || 0), 0);
        const vmConns = insts.reduce((a, i) => a + (i.conn_count || 0), 0);
        const sinkRow = (icon, name, on, connected, right) => `<div style="display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12.5px;">
            ${dot(!on ? OFF : connected ? OK : WARN)}<i class="bi ${icon}"></i> <b>${name}</b>
            <span style="color:var(--text-secondary);margin-left:auto;font-variant-numeric:tabular-nums;">${on ? right : t('status.off', 'off')}</span></div>`;
        const pipeline = `<div style="display:flex;gap:14px;align-items:stretch;flex-wrap:wrap;">
            <div class="settings-card" style="flex:1;min-width:240px;padding:12px 14px;"><div style="color:var(--text-secondary);font-size:11.5px;margin-bottom:4px;">${t('status.sources', 'Sources (polling)')} · ${devices.length}</div>${srcRows}</div>
            <div style="display:flex;align-items:center;font-size:22px;color:var(--text-secondary);">→</div>
            <div class="settings-card" style="flex:1;min-width:240px;padding:12px 14px;"><div style="color:var(--text-secondary);font-size:11.5px;margin-bottom:4px;">${t('status.sinks', 'Sinks (outputs)')}</div>
                ${sinkRow('bi-broadcast', 'MQTT', mqtt.enabled, mqtt.connected, `${(mqttRate ?? 0).toFixed(1)}/s`)}
                ${sinkRow('bi-database', 'InfluxDB', influx.enabled, influx.connected, `${(influxRate ?? 0).toFixed(1)}/s${influx.buffer_points ? ` · ⚠ ${influx.buffer_points} ${t('status.buffered', 'buffered')}` : ''}`)}
                ${sinkRow('bi-hdd-network', `${t('nav.vmeters', 'Virtual Meters')}`, insts.length > 0, insts.every(i => !i.enabled || i.running), `${insts.length} · ${vmConns} ${t('status.clients', 'clients')} · ${vmReq.toFixed(1)}/s`)}
            </div></div>`;

        // ── sparklines ──
        // With <2 samples a line can't exist yet — say so instead of a bare
        // em-dash that reads as "broken". History persists across page visits
        // (this._statusHist survives), so this only shows in the first ~4s.
        const spark = (arr) => (arr && arr.length > 1 && this._sparkline)
            ? this._sparkline(arr, 260, 40)
            : `<span class="field-hint"><i class="bi bi-hourglass-split"></i> ${t('status.collecting', 'collecting data…')}</span>`;
        const trends = `<div style="display:flex;gap:14px;flex-wrap:wrap;">
            <div class="settings-card" style="flex:1;min-width:200px;padding:10px 14px;"><div style="color:var(--text-secondary);font-size:11.5px;">${t('status.pollRate', 'Poll rate')} · ${pollRate.toFixed(1)}/s</div>${spark(this._statusHist.poll)}</div>
            <div class="settings-card" style="flex:1;min-width:200px;padding:10px 14px;"><div style="color:var(--text-secondary);font-size:11.5px;">MQTT · ${(mqttRate ?? 0).toFixed(1)}/s</div>${spark(this._statusHist.mqtt)}</div>
            <div class="settings-card" style="flex:1;min-width:200px;padding:10px 14px;"><div style="color:var(--text-secondary);font-size:11.5px;">InfluxDB · ${(influxRate ?? 0).toFixed(1)}/s</div>${spark(this._statusHist.influx)}</div></div>`;

        // ── polling / threads ──
        const pgDetail = (modbus.poll_groups_detail || []).map(g => `<tr style="color:var(--text-secondary);">
            <td style="padding:2px 12px 2px 0;">↳ ${esc(g.name)}</td><td></td>
            <td style="padding:2px 12px 2px 0;">${g.interval}s</td><td></td>
            <td style="padding:2px 12px 2px 0;">${g.age_s != null ? g.age_s + 's' : '—'}</td>
            <td style="padding:2px 0;">${g.poll_count ?? 0} polls</td></tr>`).join('');
        const latCol = ms => ms == null ? '—' : `${ms < 1 ? ms : Math.round(ms)} ms`;
        const latColor = ms => ms == null ? 'var(--text-secondary)' : ms > 1000 ? BAD : ms > 300 ? WARN : 'var(--text-secondary)';
        const pollRows = devices.map(d => `<tr>
            <td style="padding:3px 12px 3px 0;">${dot(hColor[d.data_health] || OFF)} <b>${esc(d.name || d.id)}</b></td>
            <td style="padding:3px 12px 3px 0;color:var(--text-secondary);">${esc(d.protocol || '')}</td>
            <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;">${(d.poll_rate ?? 0).toFixed ? (d.poll_rate ?? 0).toFixed(2) : d.poll_rate}/s</td>
            <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;color:${latColor(d.last_latency_ms)};">${latCol(d.last_latency_ms)}</td>
            <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;">${d.staleness_age_s != null ? d.staleness_age_s + 's' : '—'}</td>
            <td style="padding:3px 0;font-variant-numeric:tabular-nums;color:${d.failed_reads ? BAD : 'var(--text-secondary)'};">${d.failed_reads ?? 0} ${t('status.fails', 'fails')}${this._errBreakdown(d.error_counts)}</td></tr>`).join('');
        const polling = `<div class="settings-card" style="padding:12px 14px;overflow-x:auto;"><table style="width:100%;font-size:12.5px;">
            <thead><tr style="text-align:left;color:var(--text-secondary);"><th>${t('status.device', 'Device')}</th><th>${t('status.proto', 'Protocol')}</th><th>${t('status.pollRate', 'Poll rate')}</th><th>${t('status.latency', 'Latency')}</th><th>${t('status.lastRead', 'Last read')}</th><th>${t('status.readFails', 'Read fails')}</th></tr></thead>
            <tbody>${pollRows}${pgDetail}</tbody></table></div>`;

        // ── MQTT + InfluxDB cards ──
        const mqttCards = `<div style="display:flex;gap:12px;flex-wrap:wrap;">
            ${card('MQTT', mqtt.enabled ? (mqtt.connected ? t('status.connected', 'connected') : t('status.disconnected', 'disconnected')) : t('status.off', 'off'), `${esc(mqtt.broker || '')}${mqtt.port ? ':' + mqtt.port : ''}${mqtt.last_contact_age_s != null ? ` · ${t('status.lastContact', 'last')} ${mqtt.last_contact_age_s}s` : ''}`, mqtt.enabled ? (mqtt.connected ? OK : BAD) : OFF)}
            ${card(t('status.published', 'Published'), (mqtt.messages_published ?? 0).toLocaleString(), `${(mqttRate ?? 0).toFixed(1)}/s`)}
            ${card(t('status.skipped', 'Skipped'), (mqtt.messages_skipped ?? 0).toLocaleString(), t('status.unchanged', 'unchanged'))}
            ${card(t('status.failed', 'Failed'), (mqtt.messages_failed ?? 0).toLocaleString(), '', mqtt.messages_failed ? BAD : '')}
            ${card(t('status.reconnects', 'Reconnects'), mqtt.connection_count ?? 0, mqtt.disconnected_for_s ? `down ${upt(mqtt.disconnected_for_s)}` : '')}</div>`;
        const influxCards = `<div style="display:flex;gap:12px;flex-wrap:wrap;">
            ${card('InfluxDB', influx.enabled ? (influx.connected ? t('status.connected', 'connected') : t('status.disconnected', 'disconnected')) : t('status.off', 'off'), `${esc(influx.bucket || '')}${influx.last_contact_age_s != null ? ` · ${t('status.lastContact', 'last')} ${influx.last_contact_age_s}s` : ''}`, influx.enabled ? (influx.connected ? OK : BAD) : OFF)}
            ${card(t('status.written', 'Written'), (influx.writes_total ?? 0).toLocaleString(), `${(influxRate ?? 0).toFixed(1)}/s`)}
            ${card(t('status.buffer', 'Buffer (queue)'), (influx.buffer_points ?? 0).toLocaleString(), `${t('status.window', 'window')} ${influx.buffer_minutes ?? '?'}m`, influx.buffer_points ? WARN : OK)}
            ${card(t('status.replayed', 'Replayed'), (influx.replayed_total ?? 0).toLocaleString(), t('status.onReconnect', 'on reconnect'))}
            ${card(t('status.dropped', 'Dropped'), (influx.dropped_total ?? 0).toLocaleString(), '', influx.dropped_total ? BAD : '')}
            ${card(t('status.failedWrites', 'Failed'), (influx.writes_failed ?? 0).toLocaleString(), '', influx.writes_failed ? BAD : '')}</div>`;

        // ── virtual meters ──
        const vmRows = insts.map(i => {
            const conns = Array.isArray(i.connections) ? i.connections : [];
            const peers = conns.map(c => `${c.ip}:${c.port}`).join(', ') || (typeof i.peers === 'string' ? i.peers : '');
            return `<tr>
                <td style="padding:3px 12px 3px 0;">${dot(!i.enabled ? OFF : i.running ? (i.state === 'ok' ? OK : WARN) : BAD)} <b>${esc(i.name || i.template)}</b> <span class="dev-chip">:${i.port}</span></td>
                <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;">${i.conn_count ?? 0}${peers ? ` <span style="color:var(--text-secondary);">(${esc(peers)})</span>` : ''}</td>
                <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;">${(i.req_rate ?? 0).toFixed(1)}/s · ${(i.requests ?? 0).toLocaleString()}</td>
                <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;color:${i.errors ? BAD : 'var(--text-secondary)'};">${i.errors ?? 0}</td>
                <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;color:var(--text-secondary);">↓${kb(i.bytes_rx)} ↑${kb(i.bytes_tx)}</td>
                <td style="padding:3px 0;font-variant-numeric:tabular-nums;">${i.freshness_age_s != null ? i.freshness_age_s + 's' : '—'}</td></tr>`;
        }).join('') || `<tr><td style="color:var(--text-secondary);padding:6px 0;">${t('status.noVmeters', 'no virtual meters')}</td></tr>`;
        const vmeters = `<div class="settings-card" style="padding:12px 14px;overflow-x:auto;"><table style="width:100%;font-size:12.5px;">
            <thead><tr style="text-align:left;color:var(--text-secondary);"><th>${t('status.meter', 'Meter')}</th><th>${t('status.clients', 'Clients')}</th><th>${t('status.requests', 'Requests')}</th><th>${t('status.errors', 'Errors')}</th><th>RX / TX</th><th>${t('status.fresh', 'Fresh')}</th></tr></thead>
            <tbody>${vmRows}</tbody></table></div>`;

        // ── resources ──
        const resCards = res ? `<div style="display:flex;gap:12px;flex-wrap:wrap;">
            ${card('CPU', res.cpu_pct != null ? res.cpu_pct + ' %' : '…', res.num_cpus ? `${res.num_cpus} ${t('status.cores', 'cores')}` : '', res.cpu_pct > 80 ? BAD : res.cpu_pct > 50 ? WARN : '')}
            ${card(t('status.memory', 'Memory'), res.rss_mb != null ? res.rss_mb + ' MB' : '—', 'RSS')}
            ${card(t('status.threads', 'Threads'), res.threads ?? '—', '')}
            ${card(t('status.openFds', 'Open FDs'), res.open_fds ?? '—', '')}
            ${card(t('status.connections', 'TCP conns'), res.tcp_established ?? '—', t('status.established', 'established'))}
            ${card(t('status.uptime', 'Uptime'), upt(res.uptime_s), '')}</div>`
            : `<div class="field-hint">${t('status.resPending', 'Resource metrics require the updated backend (deploy pending).')}</div>`;

        // ── recent events ── prefer the persisted feed; fall back to a live merge
        let events = [];
        if (evLog && Array.isArray(evLog.events)) {
            events = evLog.events.map(e => ({ ts: e.ts, src: e.source, level: e.level, msg: e.message }));
        } else {
            (modbus.events || []).forEach(e => events.push({ ts: e.ts, src: 'Modbus', level: e.level || 'warn', msg: e.message || e.kind || 'read failure' }));
            insts.forEach(i => { if (i.last_error) events.push({ ts: i.last_error.ts || i.last_fresh, src: 'vMeter/' + (i.name || i.template), level: i.last_error.level || 'error', msg: i.last_error.message || i.last_error.kind || 'error' }); });
            events.sort((a, b) => (b.ts || 0) - (a.ts || 0));
        }
        const evColor = { error: BAD, warn: WARN, info: OFF };
        const evRows = events.slice(0, 12).map(e => `<tr>
            <td style="padding:2px 12px 2px 0;color:var(--text-secondary);white-space:nowrap;font-variant-numeric:tabular-nums;">${e.ts ? new Date(e.ts * 1000).toLocaleTimeString('en-GB') : ''}</td>
            <td style="padding:2px 8px 2px 0;"><span style="display:inline-block;padding:1px 6px;border-radius:3px;font-size:10.5px;color:#fff;background:${evColor[e.level] || OFF};">${esc(e.level)}</span></td>
            <td style="padding:2px 12px 2px 0;color:var(--text-secondary);">${esc(e.src)}</td>
            <td style="padding:2px 0;">${esc(e.msg)}</td></tr>`).join('');
        const eventsCard = `<div class="settings-card" style="padding:12px 14px;overflow-x:auto;">${events.length
            ? `<table style="width:100%;font-size:12px;"><tbody>${evRows}</tbody></table>`
            : `<span style="color:${OK};">${t('status.noEvents', 'No recent errors — everything healthy.')}</span>`}</div>`;

        host.innerHTML = banner
            + sectHead('bi-diagram-3', t('status.pipeline', 'Data pipeline')) + pipeline
            + trends
            + sectHead('bi-arrow-repeat', t('status.polling', 'Polling & threads')) + polling
            + sectHead('bi-broadcast', 'MQTT') + mqttCards
            + sectHead('bi-database', 'InfluxDB') + influxCards
            + sectHead('bi-hdd-network', t('nav.vmeters', 'Virtual Meters')) + vmeters
            + sectHead('bi-cpu', t('status.resources', 'Resources')) + resCards
            + sectHead('bi-clock-history', t('status.events', 'Recent events')) + eventsCard;

        const upd = document.getElementById('statusUpdated');
        if (upd) upd.textContent = t('status.updated', 'updated') + ' ' + new Date().toLocaleTimeString();
    },

    async _updateVmeterPill() {
        const pill = document.getElementById('statusVmeter');
        if (!pill) return;
        let h;
        try { h = await (await fetch('/health')).json(); } catch (e) { return; }
        const total = h.enabled_meters || 0;
        if (total === 0) { pill.hidden = true; return; }   // no virtual meters → hide the pill
        pill.hidden = false;
        const meters = h.meters || [];
        const online = meters.filter(m => m.state === 'ok').length;
        const down = meters.filter(m => m.state === 'down').length;
        const stale = total - online - down;
        const cnt = document.getElementById('vmeterCount');
        if (cnt) cnt.textContent = `${online}/${total}`;
        pill.classList.remove('connected', 'disconnected', 'disabled');
        if (down > 0) pill.classList.add('disconnected');        // red — a meter is genuinely down
        else if (online === total) pill.classList.add('connected'); // green — all serving + fresh
        else pill.classList.add('disabled');                     // grey — some stale (source fail-safe)
        pill.title = `Virtual meters: ${online} ok` + (stale ? `, ${stale} stale` : '')
            + (down ? `, ${down} down` : '') + ` of ${total} — click to open`;
    },

    async loadStatus() {
        try {
            const response = await fetch('/api/status');
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const status = await response.json();

            const verEl = document.getElementById('appVersion');
            if (verEl && status.version) verEl.textContent = 'v' + status.version;

            const modbus = status.modbus || {};
            const mqtt = status.mqtt || {};
            const influx = status.influxdb || {};

            this._maybeFirstRun(modbus);

            // Update status indicators in titlebar
            const statusDevices = document.getElementById('statusDevices');
            const statusMqtt = document.getElementById('statusMqtt');
            const statusInflux = document.getElementById('statusInflux');

            if (statusDevices) {
                // Online devices out of the enabled ones (the Modbus primary is
                // just one of them now). Green = all up, amber = some down, red = none.
                const enabled = (status.devices || []).filter(d => d.enabled);
                const online = enabled.filter(d => d.connected).length;
                const total = enabled.length;
                const cnt = document.getElementById('devicesOnlineCount');
                if (cnt) cnt.textContent = `${online}/${total}`;
                statusDevices.classList.toggle('connected', total > 0 && online === total);
                statusDevices.classList.toggle('partial', online > 0 && online < total);
                statusDevices.classList.toggle('disconnected', total > 0 && online === 0);
            }

            if (statusMqtt) {
                if (!mqtt.enabled) {
                    statusMqtt.classList.add('disabled');
                    statusMqtt.classList.remove('connected', 'disconnected');
                } else {
                    statusMqtt.classList.remove('disabled');
                    statusMqtt.classList.toggle('connected', mqtt.connected);
                    statusMqtt.classList.toggle('disconnected', !mqtt.connected);
                }
            }

            if (statusInflux) {
                if (!influx.enabled) {
                    statusInflux.classList.add('disabled');
                    statusInflux.classList.remove('connected', 'disconnected');
                } else {
                    statusInflux.classList.remove('disabled');
                    statusInflux.classList.toggle('connected', influx.connected);
                    statusInflux.classList.toggle('disconnected', !influx.connected);
                }
            }

            this._updateVmeterPill();

            // Update stats bar values
            const statRegisters = document.getElementById('statRegisters');
            if (statRegisters) {
                statRegisters.textContent = modbus.total_registers || '--';
            }

            const statPollRate = document.getElementById('statPollRate');
            if (statPollRate) {
                statPollRate.textContent = modbus.poll_rate || '--';
            }

            const statMqttMsg = document.getElementById('statMqttMsg');
            if (statMqttMsg) {
                statMqttMsg.textContent = (mqtt.messages_published ?? null) === null ? '--' : (mqtt.messages_published).toLocaleString();
            }

            const statInfluxPts = document.getElementById('statInfluxPts');
            if (statInfluxPts) {
                statInfluxPts.textContent = (influx.writes_total ?? null) === null ? '--' : (influx.writes_total).toLocaleString();
            }

            // Store for status page
            this.status = status;

            if (this.currentPage === 'config') {
                this.renderStatusDetails();
            }

            // Recovered: tell the user once (the heartbeat is our liveness probe).
            if (this._statusLost) {
                this._statusLost = false;
                this.showToast?.('success', 'Reconnected', 'The gateway is responding again.');
            }
        } catch (error) {
            console.error('Failed to load status:', error);
            // Surface a lost connection ONCE (not every 5s poll) so a blank/stale
            // UI isn't silent — otherwise a boot-time or mid-session backend
            // outage leaves the operator guessing.
            if (!this._statusLost) {
                this._statusLost = true;
                this.showToast?.('error', 'Connection lost',
                                 'Cannot reach the gateway — retrying every 5s…');
            }
        }
    },

    updatePollGroupsStatus() {
        // Render the status-bar poll-group intervals from the live config
        // (this.pollGroups) instead of a hardcoded label — keeps the bar in
        // sync with config/selected_registers.json (e.g. realtime 250ms).
        const el = document.getElementById('pollGroupsStatus');
        if (!el || !this.pollGroups) return;
        const icons = { realtime: 'lightning-fill', normal: 'clock', slow: 'hourglass' };
        const fmt = (s) => (s < 1 ? `${Math.round(s * 1000)}ms` : `${s}s`);
        const items = Object.entries(this.pollGroups);
        if (!items.length) return;
        el.innerHTML = items.map(([name, g]) =>
            `<span class="poll-item"><i class="bi bi-${icons[name] || 'clock'}"></i> ${this._esc(name)}: ${fmt(g.interval)}</span>`
        ).join('');
    },

    showStatusDetail(service) {
        const titleEl = document.getElementById('statusDetailTitle');
        const bodyEl = document.getElementById('statusDetailBody');

        if (!this.status) {
            bodyEl.innerHTML = `<p>${this.t('msg.statusNa', "Status not available")}</p>`;
            this.openModal('statusDetailModal');
            return;
        }

        let title = '';
        let html = '<div class="status-detail-list">';

        if (service === 'modbus') {
            const data = this.status.modbus || {};
            title = '<i class="bi bi-hdd-network"></i> Modbus Status';
            html += `
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.status', "Status")}</span>
                    <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                        ${data.connected ? 'Connected' : 'Disconnected'}
                    </span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.host', "Host")}</span>
                    <span class="status-detail-value mono">${this._esc(data.host || '-')}:${data.port || '-'}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.unitId', "Unit ID")}</span>
                    <span class="status-detail-value mono">${data.unit_id || '-'}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.measurements', "Measurements")}</span>
                    <span class="status-detail-value">${data.total_registers || 0}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.pollRate', "Poll Rate")}</span>
                    <span class="status-detail-value">${data.poll_rate || '-'}/sec</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.errors', "Errors")}</span>
                    <span class="status-detail-value ${data.errors > 0 ? 'error' : ''}">${data.errors || 0}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.dataFreshness', "Data freshness")}</span>
                    <span class="status-detail-value ${data.staleness_age_s != null && data.staleness_age_s > 10 ? 'error' : 'success'}">
                        ${data.staleness_age_s != null ? data.staleness_age_s + 's ago' : '-'}
                    </span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.lastRead', "Last successful read")}</span>
                    <span class="status-detail-value mono">${data.last_success_ts ? new Date(data.last_success_ts * 1000).toLocaleString() : '-'}</span>
                </div>
                ${(data.poll_groups_detail || []).map(g => `
                <div class="status-detail-row">
                    <span class="status-detail-label">↳ ${this._esc(g.name)} (${this._fmtInterval(g.interval)})</span>
                    <span class="status-detail-value mono">${g.age_s != null ? g.age_s + 's' : '-'} · ${g.poll_count} polls</span>
                </div>`).join('')}
                ${(data.events && data.events.length) ? `<div class="status-detail-row"><span class="status-detail-label" style="color:var(--text-secondary);">${this.t('lbl.recentFailures', "Recent read failures")}</span><span class="status-detail-value">${data.events.length}</span></div>` : ''}
                ${(data.events || []).slice(-6).reverse().map(e => `
                <div class="status-detail-row">
                    <span class="status-detail-label" style="color:#c77700;"><i class="bi bi-exclamation-triangle"></i> ${this._esc(e.kind || '')}</span>
                    <span class="status-detail-value mono" style="font-size:11px;" title="${this._esc(e.message || '')}">${new Date(e.ts * 1000).toLocaleTimeString()}</span>
                </div>`).join('')}
            `;
        } else if (service === 'mqtt') {
            const data = this.status.mqtt || {};
            title = '<i class="bi bi-broadcast"></i> MQTT Status';
            if (!data.enabled) {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.status', "Status")}</span>
                        <span class="status-detail-value">${this.t('lbl.disabled', "Disabled")}</span>
                    </div>
                `;
            } else {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.status', "Status")}</span>
                        <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                            ${data.connected ? 'Connected' : 'Disconnected'}
                        </span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.broker', "Broker")}</span>
                        <span class="status-detail-value mono">${this._esc(data.broker || '-')}:${data.port || '-'}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.prefix', "Prefix")}</span>
                        <span class="status-detail-value mono">${this._esc(data.prefix || '-')}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.messages', "Messages")}</span>
                        <span class="status-detail-value">${data.messages_published || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.skipped', "Skipped")}</span>
                        <span class="status-detail-value">${data.messages_skipped || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.mode', "Mode")}</span>
                        <span class="status-detail-value">${this._esc(data.publish_mode || 'changed')}</span>
                    </div>
                    ${data.messages_skipped > 0 ? `
                    <div class="status-detail-hint">
                        <i class="bi bi-info-circle"></i>
                        Skipped = unchanged values (publish mode: ${this._esc(data.publish_mode || 'changed')})
                    </div>
                    ` : ''}
                `;
            }
        } else if (service === 'influxdb') {
            const data = this.status.influxdb || {};
            title = '<i class="bi bi-database"></i> InfluxDB Status';
            if (!data.enabled) {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.status', "Status")}</span>
                        <span class="status-detail-value">${this.t('lbl.disabled', "Disabled")}</span>
                    </div>
                `;
            } else {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.status', "Status")}</span>
                        <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                            ${data.connected ? 'Connected' : 'Disconnected'}
                        </span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">URL</span>
                        <span class="status-detail-value mono">${this._esc(data.url || '-')}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.bucket', "Bucket")}</span>
                        <span class="status-detail-value mono">${this._esc(data.bucket || '-')}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.pointsWritten', "Points Written")}</span>
                        <span class="status-detail-value">${data.writes_total || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.skipped', "Skipped")}</span>
                        <span class="status-detail-value">${data.writes_skipped || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.errors', "Errors")}</span>
                        <span class="status-detail-value ${data.writes_failed > 0 ? 'error' : ''}">${data.writes_failed || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">${this.t('lbl.mode', "Mode")}</span>
                        <span class="status-detail-value">${this._esc(data.publish_mode || 'changed')}</span>
                    </div>
                    ${data.writes_skipped > 0 ? `
                    <div class="status-detail-hint">
                        <i class="bi bi-info-circle"></i>
                        Skipped = ${data.publish_mode === 'changed' ? 'unchanged values' : 'rate limited'} (publish mode: ${this._esc(data.publish_mode || 'changed')})
                    </div>
                    ` : ''}
                `;
            }
        }

        html += '</div>';
        titleEl.innerHTML = title;
        bodyEl.innerHTML = html;
        this.openModal('statusDetailModal');
    },

    closeStatusDetailModal() {
        this.closeModal('statusDetailModal');
    },

    renderPollGroups() {
        const container = document.getElementById('pollGroups');
        if (!container) return;
        container.innerHTML = '';

        for (const [name, config] of Object.entries(this.pollGroups)) {
            const div = document.createElement('div');
            div.className = 'poll-group-card';
            div.innerHTML = `
                <div class="name">${this._esc(name)}</div>
                <div class="interval">${this._esc(String(config.interval))}s</div>
                <div class="desc">${this._esc(config.description || '')}</div>
            `;
            container.appendChild(div);
        }
    },

    // Fire a synthetic alert to verify the configured channels (MQTT + webhook).
    async testAlert(btn) {
        const out = document.getElementById('alertTestResult');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            const r = await fetch('/api/alerts/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            const res = await r.json();
            if (!r.ok) {
                const msg = (res.detail?.errors || [res.detail || 'failed']).join('; ');
                if (out) out.innerHTML = `<span style="color:var(--danger-text,#c0392b);">${this._esc(msg)}</span>`;
                return;
            }
            const parts = Object.entries(res.channels || {}).map(([k, v]) => `${k}: ${v}`);
            const okAll = parts.length && parts.every(p => p.includes('sent'));
            if (out) out.innerHTML = res.delivered
                ? `<span style="color:${okAll ? 'var(--success-text,#1a8f4c)' : 'var(--warning-text,#c77700)'};">${this._esc(parts.join(' · '))}</span>`
                : `<span style="color:var(--text-secondary);">${this.t('status.noChannel', 'no channel')}</span>`;
            this.showToast(res.delivered && okAll ? 'success' : 'warning',
                           this.t('status.testAlert', 'Test'), parts.join(' · ') || this.t('status.noChannel', 'no channel'));
        } catch (e) {
            if (out) out.innerHTML = `<span style="color:var(--danger-text,#c0392b);">${this._esc(e.message)}</span>`;
        } finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    },

    renderStatusDetails() {
        const container = document.getElementById('statusDetails');
        if (!container || !this.status) return;

        const modbus = this.status.modbus || {};
        const mqtt = this.status.mqtt || {};
        const influx = this.status.influxdb || {};

        container.innerHTML = `
            <div class="status-block">
                <h4>
                    <span class="status-indicator ${modbus.connected ? 'ok' : 'error'}"></span>
                    Modbus
                </h4>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.host', "Host")}</span>
                    <span class="value">${this._esc(modbus.host)}:${modbus.port}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.successfulReads', "Successful reads")}</span>
                    <span class="value">${modbus.successful_reads || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.failedReads', "Failed reads")}</span>
                    <span class="value">${modbus.failed_reads || 0}</span>
                </div>
            </div>

            <div class="status-block">
                <h4>
                    <span class="status-indicator ${mqtt.connected ? 'ok' : (mqtt.enabled ? 'error' : '')}"></span>
                    MQTT ${mqtt.enabled ? '' : '(Disabled)'}
                </h4>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.broker', "Broker")}</span>
                    <span class="value">${this._esc(mqtt.broker)}:${mqtt.port}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.published', "Published")}</span>
                    <span class="value">${mqtt.messages_published || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.skipped', "Skipped")}</span>
                    <span class="value">${mqtt.messages_skipped || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.mode', "Mode")}</span>
                    <span class="value">${this._esc(mqtt.publish_mode || '-')}</span>
                </div>
            </div>

            <div class="status-block">
                <h4>
                    <span class="status-indicator ${influx.connected ? 'ok' : (influx.enabled ? 'error' : '')}"></span>
                    InfluxDB ${influx.enabled ? '' : '(Disabled)'}
                </h4>
                <div class="detail-row">
                    <span class="label">URL</span>
                    <span class="value">${this._esc(influx.url || '-')}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.bucket', "Bucket")}</span>
                    <span class="value">${this._esc(influx.bucket || '-')}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.writes', "Writes")}</span>
                    <span class="value">${influx.writes_total || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">${this.t('lbl.mode', "Mode")}</span>
                    <span class="value">${this._esc(influx.publish_mode || '-')}</span>
                </div>
            </div>
        `;
    }
});
