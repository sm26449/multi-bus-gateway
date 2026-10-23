/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// status domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ── Status page ───────────────────────────────────────────────────────
    _stopStatusPoll() {
        // OWN handle: this used to clear this._statusTimer — the app-wide 5 s
        // loadStatus loop from init() — so one visit to the Status page froze
        // the titlebar health dots and the connection-lost detection for the
        // rest of the session (audit 2026-10-01)
        if (this._statusPageTimer) { clearInterval(this._statusPageTimer); this._statusPageTimer = null; }
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
        return ` <span class="st-faint">· ${parts.join(' · ')}</span>`;
    },


    // ── Status page ─────────────────────────
    // A frame built once (per language), regions morphed in place every 2 s,
    // and one judgement of what needs attention: StatusModel (status-model.js).

    initStatusPage() {
        const s = this._st = this._st || { data: {}, at: {}, tried: {}, failed: {}, inflight: {}, memo: {}, hist: [], prev: null };
        const TABS = ['devices', 'installations', 'outputs', 'automation', 'gateway', 'events'];
        if (!TABS.includes(s.tab)) s.tab = this._stLoad('mbg-status-tab', 'devices');
        if (!TABS.includes(s.tab)) s.tab = 'devices';
        if (!s.devFilter) s.devFilter = this._stLoad('mbg-status-devfilter', 'all');
        if (!['all', 'error', 'warn', 'info'].includes(s.evFilter)) s.evFilter = this._stLoad('mbg-status-evfilter', 'all');
        if (!['all', 'error', 'warn', 'info'].includes(s.evFilter)) s.evFilter = 'all';
        if (!document.getElementById('stTabs') || s.frameLang !== this._lang) this._statusFrame();
        this.renderStatus();
        this._stopStatusPoll();
        this._statusPageTimer = setInterval(() => {
            if (this.currentPage === 'status' && !document.hidden) this.renderStatus(false);
        }, 2000);
    },

    _stLoad(key, dflt) { try { return localStorage.getItem(key) || dflt; } catch (e) { return dflt; } },
    _stSave(key, v) { try { localStorage.setItem(key, v); } catch (e) { /* private window */ } },

    _statusFrame() {
        const host = document.getElementById('statusContent');
        if (!host) return;
        const t = this.t.bind(this), s = this._st;
        s.frameLang = this._lang;
        const tabs = [
            ['devices', 'bi-hdd-stack', t('status.tab.devices', 'Devices')],
            ['installations', 'bi-diagram-3', t('status.tab.installations', 'Installations')],
            ['outputs', 'bi-broadcast', t('status.tab.outputs', 'Outputs')],
            ['automation', 'bi-lightning-charge', t('status.tab.automation', 'Automation')],
            ['gateway', 'bi-cpu', t('status.tab.gateway', 'Gateway')],
            ['events', 'bi-clock-history', t('status.tab.events', 'Events')],
        ];
        const seg = (id, action, opts, cur) => `<div class="st-seg" role="group" id="${id}">${opts.map(([v, label]) =>
            `<button type="button" ${this._act(action, [v])} aria-pressed="${v === cur}" data-v="${v}">${label}</button>`).join('')}</div>`;
        const panel = (name, inner) => `<div class="st-panel" role="tabpanel" id="stPanel-${name}" aria-labelledby="stTab-${name}" tabindex="0"${name === s.tab ? '' : ' hidden'}>${inner}</div>`;
        host.innerHTML = `
            <div id="stHead"></div>
            <div class="st-pills" id="stPills"></div>
            <section class="st-attn" id="stAttention" aria-label="${this._esc(t('status.attention', 'Needs attention'))}"></section>
            <div class="sr-only" id="stLive" aria-live="polite"></div>
            <div class="st-tabs" role="tablist" id="stTabs" aria-label="${this._esc(t('status.title', 'System Status'))}">${tabs.map(([name, icon, label]) =>
                `<button type="button" role="tab" class="st-tab" id="stTab-${name}" aria-controls="stPanel-${name}" aria-selected="${name === s.tab}" tabindex="${name === s.tab ? 0 : -1}" ${this._act('statusTab', [name])}><i aria-hidden="true" class="bi ${icon}"></i> ${label} <span class="st-tab-mark" id="stMark-${name}"></span></button>`).join('')}</div>
            ${panel('devices', `<div class="st-toolbar">
                    <input type="search" class="st-search" id="stDevSearch" placeholder="${this._esc(t('status.search', 'Search a device or installation…'))}" aria-label="${this._esc(t('status.search', 'Search a device or installation…'))}">
                    ${seg('stDevFilter', 'statusDevFilter', [['all', t('status.f.all', 'All')], ['problems', t('status.f.problems', 'Problems')], ['disabled', t('status.f.disabled', 'Disabled')]], s.devFilter)}
                    <span class="st-count" id="stDevCount"></span></div>
                <div class="st-card st-scroll" id="stDevices"></div>`)}
            ${panel('installations', `<div id="stInstallations"></div>`)}
            ${panel('outputs', `<div id="stOutputs"></div>
                <div class="st-card" id="stAlertsBox">
                    <div class="st-out-head"><span class="st-contents" id="stAlertHead"></span>
                        <button type="button" id="statusTestAlertBtn" class="btn btn-ghost btn-sm" data-live data-action="testAlert" data-with-el hidden><i aria-hidden="true" class="bi bi-send"></i> ${t('status.testAlert', 'Test')}</button> <span id="alertTestResultStatus"></span>
                    </div>
                    <div id="stAlertBody"></div>
                </div>
                <div id="stRouting"></div>`)}
            ${panel('automation', `<div id="stAutomation"></div>`)}
            ${panel('gateway', `<div id="stGateway"></div>`)}
            ${panel('events', `<div class="st-toolbar">
                    <input type="search" class="st-search" id="stEvSearch" placeholder="${this._esc(t('status.evSearch', 'Search the source or the message…'))}" aria-label="${this._esc(t('status.evSearch', 'Search the source or the message…'))}">
                    <select class="st-search st-select" id="stEvSource" aria-label="${this._esc(t('status.source', 'Source'))}"><option value="">${t('status.allSources', 'All sources')}</option></select>
                    ${seg('stEvFilter', 'statusEvFilter', [['all', t('status.f.all', 'All')], ['error', t('status.f.errors', 'Errors')], ['warn', t('status.f.warnings', 'Warnings')], ['info', t('status.f.info', 'Info')]]
                        .map(([v, label]) => [v, `${label} <span class="st-seg-n" id="stEvN-${v}"></span>`]), s.evFilter)}
                    <span class="st-count" id="stEvCount"></span></div>
                <div class="st-card st-evscroll" id="stEventsBox" tabindex="0" aria-label="${this._esc(t('status.tab.events', 'Events'))}"><div id="stEvents"></div></div>`)}`;
        const evSearch = document.getElementById('stEvSearch');
        if (evSearch) {
            evSearch.value = s.evSearch || '';
            evSearch.addEventListener('input', () => { s.evSearch = evSearch.value; this._statusPaint(); });
        }
        const evSource = document.getElementById('stEvSource');
        if (evSource) evSource.addEventListener('change', () => { s.evSource = evSource.value; this._statusPaint(); });
        s.evSources = null;
        const search = document.getElementById('stDevSearch');
        if (search) {
            search.value = s.search || '';
            search.addEventListener('input', () => { s.search = search.value; this._statusPaint(); });
        }
        // the ARIA tabs pattern: arrows, Home and End move between the tabs
        document.getElementById('stTabs')?.addEventListener('keydown', ev => {
            const names = ['devices', 'installations', 'outputs', 'automation', 'gateway', 'events'];
            const i = names.indexOf(s.tab);
            const j = ev.key === 'ArrowRight' ? (i + 1) % names.length : ev.key === 'ArrowLeft' ? (i + names.length - 1) % names.length
                : ev.key === 'Home' ? 0 : ev.key === 'End' ? names.length - 1 : -1;
            if (j < 0) return;
            ev.preventDefault();
            this.statusTab(names[j]);
            document.getElementById('stTab-' + names[j])?.focus();
        });
        s.lastCounts = null;
    },

    statusTab(name) {
        const s = this._st;
        if (!s) return;
        s.tab = name;
        this._stSave('mbg-status-tab', name);
        document.querySelectorAll('#stTabs .st-tab').forEach(b => {
            const on = b.id === 'stTab-' + name;
            b.setAttribute('aria-selected', on);
            b.tabIndex = on ? 0 : -1;
        });
        document.querySelectorAll('#statusContent .st-panel').forEach(p => { p.hidden = p.id !== 'stPanel-' + name; });
        this._statusPaint();
    },

    statusDevFilter(v) { this._st.devFilter = v; this._stSave('mbg-status-devfilter', v); this._stSeg('stDevFilter', v); this._statusPaint(); },
    // a click on an event's source filters on it
    statusEvSource(src) {
        const s = this._st;
        s.evSource = src;
        const sel = document.getElementById('stEvSource');
        if (sel) { this._stEvSourceOptions(true); sel.value = src; }
        this._statusPaint();
    },
    statusEvFilter(v) { this._st.evFilter = v; this._stSave('mbg-status-evfilter', v); this._stSeg('stEvFilter', v); this._statusPaint(); },
    _stSeg(id, v) { document.querySelectorAll(`#${id} button`).forEach(b => b.setAttribute('aria-pressed', b.dataset.v === v)); },

    // Fetch what is due — /api/status and /health every 2 s, the rest slower
    // (§3). Each route on its own: one still running is not asked again, none
    // waits more than 5 s, and a slow one never throws away a faster one's
    // answer. A route that fails keeps its last answer; only its part says so,
    // and it is asked again at its own pace, not every tick.
    async renderStatus(force = true) {
        const s = this._st;
        if (!s || !document.getElementById('statusContent')) return;
        const now = Date.now() / 1000;
        const routes = [
            ['status', '/api/status', 2], ['health', '/health', 2],
            ['vm', '/api/virtual-meters', 5], ['res', '/api/status/resources', 5],
            ['rules', '/api/rules', 10], ['sequences', '/api/sequences', 10], ['bridges', '/api/bridges', 10],
            ['opcua', '/api/opcua', 10], ['bus', '/api/role/bus', 10],
            ['alerts', '/api/alerts?limit=10', 10], ['events', '/api/events?limit=300', 10],
        ].filter(([key, , every]) => !s.inflight[key] && (force || !s.tried[key] || now - s.tried[key] >= every - 0.5));
        await Promise.all(routes.map(async ([key, url]) => {
            s.inflight[key] = true;
            s.tried[key] = now;
            const ctl = new AbortController();
            const timer = setTimeout(() => ctl.abort(), 5000);
            let val;
            try {
                const r = await fetch(url, { signal: ctl.signal });
                if (key === 'opcua' && r.status === 503) val = { unavailable: true };
                else if (!r.ok) throw new Error('HTTP ' + r.status);
                else val = await r.json();
            } catch (e) { val = undefined; }
            finally { clearTimeout(timer); s.inflight[key] = false; }
            if (val === undefined) { s.failed[key] = true; return; }
            if (s.at[key] && s.at[key] > now) return;          // a newer answer already landed
            s.failed[key] = false;
            s.at[key] = now;
            if (key === 'status') this._stRates(val, now);
            s.data[key] = val;
            if (this.currentPage === 'status') this._statusPaint();
        }));
        if (this.currentPage === 'status') this._statusPaint();     // the "no answer" clock
    },

    // rates between two /api/status answers; the reads sparkline's history
    _stRates(st, now) {
        const s = this._st, mqtt = st.mqtt || {}, influx = st.influxdb || {};
        const confirmed = influx.writes_confirmed ?? influx.writes_total ?? 0;
        const p = s.prev;
        if (p && now > p.t) {
            const dt = now - p.t;
            s.mqttRate = Math.max(0, ((mqtt.messages_published || 0) - p.mqtt) / dt);
            s.influxRate = Math.max(0, (confirmed - p.influx) / dt);
        }
        s.prev = { t: now, mqtt: mqtt.messages_published || 0, influx: confirmed };
        const reads = (st.devices || []).reduce((a, d) => a + (d.poll_rate || 0), 0);
        s.hist.push(Math.round(reads * 100) / 100);
        if (s.hist.length > 90) s.hist.shift();
    },

    _statusPaint() {
        const s = this._st, D = s && s.data;
        if (!D || !D.status || !window.StatusModel || !document.getElementById('stHead')) return;
        const now = Date.now() / 1000;
        const m = s.model = StatusModel.assess({
            now, sample: s.at.status, status: D.status, health: D.health, vm: (D.vm && D.vm.instances) || [], res: D.res,
            rules: D.rules && D.rules.rules, sequences: D.sequences && D.sequences.sequences,
            bridges: D.bridges && D.bridges.bridges, opcua: D.opcua, alerts: D.alerts, events: D.events,
        }, s.memo);
        const set = (id, html) => { const el = document.getElementById(id); if (el) this._morph(el, html); };
        set('stHead', this._stHead(m, now));
        set('stPills', this._stPills(m));
        set('stAttention', this._stAttention(m));
        for (const tab of StatusModel.TABS) {
            const mark = document.getElementById('stMark-' + tab), sev = m.pills[tab].sev;
            if (mark) {
                const glyph = sev === 'error' ? '✖' : sev === 'warn' ? '▲' : '';
                const word = glyph ? this.t(sev === 'error' ? 'status.hasErrors' : 'status.hasWarnings', sev === 'error' ? 'has errors' : 'has warnings') : '';
                this._morph(mark, glyph ? `<span aria-hidden="true">${glyph}</span><span class="sr-only">, ${this._esc(word)}</span>` : '');
                mark.className = 'st-tab-mark' + (glyph ? ' st-' + sev : '');
                mark.title = word;
            }
        }
        // the polite live region speaks only when the counts change
        const counts = `${m.errors}/${m.warnings}`;
        if (s.lastCounts !== null && counts !== s.lastCounts) {
            const live = document.getElementById('stLive');
            if (live) live.textContent = this._stHeadline(m);
        }
        s.lastCounts = counts;
        // only the shown tab is drawn; the others when they are opened
        const tab = s.tab;
        if (tab === 'devices') set('stDevices', this._stDevices(m));
        else if (tab === 'installations') set('stInstallations', this._stInstallations(m));
        else if (tab === 'outputs') { set('stOutputs', this._stOutputs(m)); this._stAlerts(set); this._stRouting(set); }
        else if (tab === 'automation') set('stAutomation', this._stAutomation(m));
        else if (tab === 'gateway') set('stGateway', this._stGateway(m));
        else if (tab === 'events') {
            // new events arrive on top: a reader scrolled down keeps their place
            const box = document.getElementById('stEventsBox');
            const top = box ? box.scrollTop : 0, h = box ? box.scrollHeight : 0;
            set('stEvents', this._stEvents());
            if (box && top > 0) box.scrollTop = top + (box.scrollHeight - h);
        }
        const upd = document.getElementById('statusUpdated');
        if (upd && s.at.status) upd.textContent = this.t('status.updated', 'updated') + ' ' + this._stTime(s.at.status);
    },

    // ── small formatters ──
    _stTime(ts) {
        try { return new Date(ts * 1000).toLocaleTimeString(this._loc(), { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
        catch (e) { return new Date(ts * 1000).toLocaleTimeString(this._loc()); }
    },
    _stDur(sec) {
        if (sec == null || !isFinite(sec)) return '—';
        sec = Math.max(0, Math.floor(sec));
        const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
        return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${sec}s`;
    },
    _stNum(n, digits = 0) { return n == null || !isFinite(n) ? '—' : Number(n).toLocaleString(this._loc(), { maximumFractionDigits: digits, minimumFractionDigits: digits }); },
    _stDot(cls, word) { return `<span class="st-dot st-c-${cls}"><span class="st-dot-word">${this._esc(word)}</span></span>`; },
    _stWordCls(word) {
        return ({ ok: 'ok', online: 'ok', running: 'ok', connected: 'ok', degraded: 'warn', stale: 'warn', partial: 'warn',
                  down: 'error', offline: 'error', asleep: 'off', disabled: 'off', idle: 'off', standby: 'info', unknown: 'off' })[word] || 'off';
    },
    _stWord(word) { return this.t('status.word.' + word, word); },
    _stLate(key, every) {
        const s = this._st;
        if (!s.failed[key]) return '';
        const at = s.at[key];
        return ` <span class="st-warn-t" title="${this._esc(this.t('status.notUpdated', 'not updated'))}">${at
            ? this.t('status.notUpdatedSince', 'not updated since {t}', { t: this._stTime(at) })
            : this.t('status.unavailable', 'not available')}</span>`;
    },
    _stWhat(i) {
        const w = i.what || {};
        return this.t(w.k, w.d, w.p) + (w.extra ? ' · ' + w.extra : '');
    },
    _stHeadline(m) {
        const t = this.t.bind(this);
        if (m.errors) return this.tn('status.nErrors', m.errors, '{n} errors') + (m.warnings ? ' · ' + this.tn('status.nWarnings', m.warnings, '{n} warnings') : '');
        if (m.warnings) return this.tn('status.nWarnings', m.warnings, '{n} warnings');
        return m.standby ? t('status.standbyTitle', 'Standby') : t('status.allOk', 'All systems operational');
    },

    // ── regions ──
    _stHead(m, now) {
        const t = this.t.bind(this), s = this._st, st = s.data.status || {}, res = s.data.res || {};
        const cls = m.errors ? 'st-error' : m.warnings ? 'st-warn' : m.standby ? 'st-standby' : '';
        const icon = m.errors ? 'bi-x-octagon-fill st-bad' : m.warnings ? 'bi-exclamation-triangle-fill st-warn-t' : m.standby ? 'bi-pause-circle' : 'bi-check-circle-fill';
        const sub = m.standby
            ? t('status.standbySub', 'Standby — the active gateway polls the devices; this one mirrors its configuration and waits.')
            : m.errors || m.warnings ? t('status.subIssues', 'The list below leads to each one.')
            : t('status.subOk', 'Every enabled device, output and rule is working.');
        const age = s.at.status ? now - s.at.status : null;
        // the section header already says when; only a silence is news here
        const late = age != null && age > 10
            ? `<span class="st-late">${t('status.noAnswer', 'no answer for {s}', { s: this._stDur(age) })}</span>` : '';
        const role = m.role && m.role.role;
        return `<div class="st-head ${cls}">
            <div class="st-head-state">
                <div class="st-head-title"><i aria-hidden="true" class="bi ${icon}"></i> ${this._esc(this._stHeadline(m))}</div>
                <div class="st-head-sub">${this._esc(sub)}</div>
            </div>
            <div class="st-head-meta">
                <span>v${this._esc(st.version || '')} · ${t('status.uptime', 'Uptime')} ${this._stDur(res.uptime_s)}${role ? ` · <span class="st-role${role === 'standby' ? ' st-role-standby' : ''}">${this._esc(t('status.role.' + role, role).toUpperCase())}</span>` : ''}</span>
                ${late}
            </div></div>`;
    },

    _stPills(m) {
        const t = this.t.bind(this), P = m.pills, cur = this._st.tab;
        const pill = (tab, icon, label, value, sub, off) => {
            const sev = P[tab].sev;
            const cls = off ? 'st-off' : sev === 'error' ? 'st-error' : sev === 'warn' ? 'st-warn' : '';
            const word = sev === 'error' ? t('status.hasErrors', 'has errors') : sev === 'warn' ? t('status.hasWarnings', 'has warnings') : '';
            return `<button type="button" class="st-pill ${cls}" data-key="${tab}" ${this._act('statusTab', [tab])}${cur === tab ? ' aria-current="true"' : ''}
                aria-label="${this._esc(`${label}: ${value}${sub ? ', ' + sub : ''}${word ? ', ' + word : ''}`)}">
                <span class="st-pill-label"><i aria-hidden="true" class="bi ${icon}"></i> ${this._esc(label)}${sev === 'error' ? ' <span class="st-bad">✖</span>' : sev === 'warn' ? ' <span class="st-warn-t">▲</span>' : ''}</span>
                <span class="st-pill-value">${this._esc(value)}</span>
                <span class="st-pill-sub">${this._esc(sub || '')}</span></button>`;
        };
        const d = P.devices, i = P.installations, o = P.outputs, a = P.automation, g = P.gateway;
        // a standby polls nothing: "0/24 online" would read as an outage
        const sb = m.standby ? this._stWord('standby') : '';
        return pill('devices', 'bi-hdd-stack', t('status.tab.devices', 'Devices'), sb || (d.of ? `${d.n}/${d.of}` : '—'),
                    sb ? t('status.standbyPolls', 'the active gateway polls') : d.asleep ? this.tn('status.nAsleep', d.asleep, '{n} asleep') : t('status.online', 'online'), !d.of || !!sb)
            + pill('installations', 'bi-diagram-3', t('status.tab.installations', 'Installations'), sb || (i.of ? `${i.n}/${i.of}` : '—'),
                    sb ? t('status.standbyPolls', 'the active gateway polls') : i.of ? t('status.online', 'online') : t('status.none', 'none'), !i.of || !!sb)
            + pill('outputs', 'bi-broadcast', t('status.tab.outputs', 'Outputs'), o.of ? `${o.n}/${o.of}` : '—',
                    o.of ? t('status.delivering', 'delivering') : t('status.none', 'none'), !o.of)
            + pill('automation', 'bi-lightning-charge', t('status.tab.automation', 'Automation'),
                    this.tn('status.nArmed', a.armed, '{n} armed'),
                    t('status.nShadow', '{n} shadow', { n: a.shadow }) + (a.seqRunning ? ' · ' + t('status.nRunning', '{n} running', { n: a.seqRunning }) : ''))
            + pill('gateway', 'bi-cpu', t('status.tab.gateway', 'Gateway'), g.cpu != null ? `${this._stNum(g.cpu, 1)} % CPU` : '—',
                    g.rss != null ? `${this._stNum(g.rss)} MB` : '');
    },

    _stAttention(m) {
        if (!m.issues.length) return '';
        const t = this.t.bind(this), MAX = 6;
        const rows = m.issues.slice(0, MAX).map(i => {
            const sevWord = i.sev === 'error' ? t('status.sev.error', 'Error') : t('status.sev.warn', 'Warning');
            const since = i.since != null ? (i.seen ? t('status.seenFor', 'seen {d}', { d: this._stDur(i.since) }) : this._stDur(i.since)) : '';
            return `<li class="st-issue st-${i.sev}" data-key="${this._esc(i.key)}">
                <span class="st-issue-icon" aria-hidden="true">${i.sev === 'error' ? '✖' : '▲'}</span><span class="sr-only">${sevWord}:</span>
                <span class="st-issue-name" title="${this._esc(i.name)}">${this._esc(i.name)}</span>
                <span class="st-issue-what" title="${this._esc(this._stWhat(i))}">${this._esc(this._stWhat(i))}</span>
                <span class="st-issue-since" title="${this._esc(i.seen ? t('status.seenTip', 'since this page first saw it') : t('status.inStateTip', 'time in this state'))}">${this._esc(since)}</span>
                ${i.open ? `<button type="button" class="btn btn-ghost btn-sm" ${this._act(i.open.method, i.open.args)} aria-label="${this._esc(t('status.openX', 'Open {x}', { x: i.name }))}">${t('status.open', 'Open')} <i aria-hidden="true" class="bi bi-arrow-right"></i></button>` : ''}
            </li>`;
        }).join('');
        const more = m.issues.length > MAX
            ? `<div class="st-attn-more">${t('status.andMore', 'and {n} more — the pills and tabs marked ✖ ▲ hold them', { n: m.issues.length - MAX })}</div>` : '';
        return `<div class="st-attn-box"><div class="st-attn-title">${t('status.attention', 'Needs attention')} · ${m.issues.length}</div><ul>${rows}</ul>${more}</div>`;
    },

    _stDevices(m) {
        const t = this.t.bind(this), s = this._st, st = s.data.status || {};
        const instName = new Map((st.endpoints || []).map(e => [e.id, e.name || e.id]));
        const q = (s.search || '').trim().toLowerCase();
        const all = m.devices;
        const rows = all.filter(d => {
            if (s.devFilter === 'problems' && d.sev === 'ok') return false;
            if (s.devFilter === 'disabled' && d.word !== 'disabled') return false;
            if (s.devFilter === 'all' && d.word === 'disabled') return false;
            if (!q) return true;
            return [d.name, d.id, instName.get(d.endpoint_id)].some(x => String(x || '').toLowerCase().includes(q));
        });
        const cnt = document.getElementById('stDevCount');
        if (cnt) cnt.textContent = t('status.nOfM', '{n} of {m}', { n: rows.length, m: all.length });
        if (!rows.length) {
            return `<p class="st-muted" style="margin:6px 0;">${!all.length ? t('status.noDevices', 'no devices')
                : s.devFilter === 'problems' && !q ? t('status.noProblems', 'No device has a problem.') : t('status.noMatch', 'Nothing matches.')}</p>`;
        }
        const modbus = st.modbus || {};
        const proto = d => (d.read_via || []).length ? [...new Set(d.read_via.map(r => r.protocol))].join(' + ') : (d.protocol || '');
        const body = rows.map(d => {
            const word = d.slow && d.word === 'ok' ? 'slow' : d.word;
            const cls = d.slow && d.word === 'ok' ? 'warn' : this._stWordCls(d.word);
            const lat = d.last_latency_ms == null ? '—' : `${d.last_latency_ms < 1 ? d.last_latency_ms : Math.round(d.last_latency_ms)} ms`;
            const fails = d.failed_reads || 0;
            const inst = d.endpoint_id ? `<span class="st-chip">${this._esc(instName.get(d.endpoint_id) || d.endpoint_id)}</span>` : '';
            const row = `<tr data-key="${this._esc(d.id)}" tabindex="0" data-key-enter ${this._act('openDeviceDetail', [d.id])}
                    aria-label="${this._esc(`${d.name || d.id}: ${this._stWord(word)}`)}">
                <td>${this._stDot(cls, this._stWord(word))}</td>
                <td><span class="st-name">${this._esc(d.name || d.id)}</span>${inst}</td>
                <td class="st-muted st-hide-sm">${this._esc(proto(d))}</td>
                <td class="num st-hide-sm">${d.poll_rate != null ? this._stNum(d.poll_rate, 2) + '/s'
                    : /mqtt|tap/.test(proto(d)) ? `<span class="st-muted" title="${this._esc(t('status.pushTip', 'It sends its values; nothing is polled'))}">${t('status.push', 'push')}</span>` : '—'}</td>
                <td class="num st-hide-sm${d.slow ? ' st-warn-t' : ''}">${lat}</td>
                <td class="num">${d.staleness_age_s != null ? this._stDur(d.staleness_age_s) : '—'}</td>
                <td class="num${fails ? ' st-bad' : ' st-muted'}">${this._stNum(fails)}<span class="st-hide-sm">${this._errBreakdown(d.error_counts)}</span></td>
                <td class="num st-muted st-hide-sm" title="${this._esc(t('status.upSinceTip', 'time in the current health state (connection uptime)'))}">${d.up_since_s != null ? this._stDur(d.up_since_s) : '—'}</td></tr>`;
            const groups = d.primary && (modbus.poll_groups_detail || []).length
                ? `<tr class="st-sub" data-key="${this._esc(d.id)}:groups"><td></td><td colspan="7">${modbus.poll_groups_detail.map(g =>
                    `${this._esc(g.name)} ${this._esc(this._fmtInterval ? this._fmtInterval(g.interval) : g.interval + 's')} · ${g.age_s != null ? this._stDur(g.age_s) : '—'}`).join(' &nbsp;·&nbsp; ')}</td></tr>` : '';
            return row + groups;
        }).join('');
        return `<table class="st-table"><thead><tr>
            <th scope="col">${t('status.health', 'Health')}</th><th scope="col">${t('status.device', 'Device')}</th>
            <th scope="col" class="st-hide-sm">${t('status.proto', 'Protocol')}</th><th scope="col" class="st-hide-sm">${t('status.reads', 'Reads')}</th>
            <th scope="col" class="st-hide-sm">${t('status.latency', 'Latency')}</th><th scope="col">${t('status.lastRead', 'Last read')}</th>
            <th scope="col">${t('status.readFails', 'Read fails')}</th><th scope="col" class="st-hide-sm">${t('status.inState', 'In state')}</th>
            </tr></thead><tbody>${body}</tbody></table>`;
    },

    _stInstallations(m) {
        const t = this.t.bind(this);
        if (!m.installations.length) {
            return `<p class="st-muted">${t('status.noInstallations', 'No installations. An installation groups inverters, a battery bank or meters and adds up their totals.')}</p>`;
        }
        return `<div class="st-grid">${m.installations.map(e => {
            const cls = this._stWordCls(e.word);
            return `<button type="button" class="st-card st-inst st-c-${cls}" data-key="${this._esc(e.id)}" ${this._act('openEndpointDetail', [e.id])}
                    aria-label="${this._esc(`${e.name || e.id}: ${this._stWord(e.word)}`)}">
                <div class="st-out-head"><span class="st-out-name">${this._esc(e.name || e.id)}</span> ${this._stDot(cls, this._stWord(e.word))}</div>
                <dl class="st-kv">
                    <div><dt>${t('status.units', 'Units')}</dt><dd>${this._stNum(e.units_online || 0)}/${this._stNum(e.units_total || 0)} ${t('status.online', 'online')}</dd></div>
                    ${e.units_asleep != null ? `<div><dt>${t('status.word.asleep', 'asleep')}</dt><dd>${this._stNum(e.units_asleep)}</dd></div>` : ''}
                    ${e.aggregates_enabled === false ? `<div><dt>${t('status.totals', 'Totals')}</dt><dd>${t('status.off', 'off')}</dd></div>` : ''}
                </dl></button>`;
        }).join('')}</div>`;
    },

    _stOutputs(m) {
        const t = this.t.bind(this), s = this._st, D = s.data, st = D.status || {};
        const mqtt = st.mqtt || {}, influx = st.influxdb || {}, op = D.opcua, insts = (D.vm && D.vm.instances) || [];
        const kv = items => `<dl class="st-kv">${items.filter(Boolean).map(([k, v, cls]) =>
            `<div><dt>${this._esc(k)}</dt><dd${cls ? ` class="${cls}"` : ''}>${v}</dd></div>`).join('')}</dl>`;
        const head = (key, icon, name, on, ok, word, detail) => `<div class="st-out-head">
            <i aria-hidden="true" class="bi ${icon}"></i><span class="st-out-name">${name}</span>
            ${!on ? this._stDot('off', t('status.off', 'off')) : this._stDot(ok ? 'ok' : 'error', word)}
            ${detail ? `<span class="st-muted">${detail}</span>` : ''}</div>`;
        const offLine = (cfgtab, label) => `<div class="st-out-off">${t('status.offHint', 'Not enabled.')} <button type="button" class="btn btn-ghost btn-sm" ${cfgtab === 'vmeters' ? this._act('navigateTo', ['vmeters']) : this._act('openSettingsTab', [cfgtab])}>${label} <i aria-hidden="true" class="bi bi-arrow-right"></i></button></div>`;
        const settings = t('nav.config', 'Settings');

        const mq = `<div class="st-card" data-key="mqtt">${head('mqtt', 'bi-broadcast', 'MQTT', mqtt.enabled, mqtt.connected,
                mqtt.connected ? t('status.connected', 'connected') : t('status.disconnected', 'disconnected'),
                this._esc(`${mqtt.broker || ''}${mqtt.port ? ':' + mqtt.port : ''}`))}
            ${mqtt.enabled ? kv([
                [t('status.published', 'Published'), `${this._stNum(mqtt.messages_published)} <span class="st-faint">${this._stNum(s.mqttRate, 1)}/s</span>`],
                [t('status.skipped', 'Skipped'), this._stNum(mqtt.messages_skipped)],
                [t('status.failed', 'Failed'), this._stNum(mqtt.messages_failed || 0), mqtt.messages_failed ? 'st-bad' : ''],
                mqtt.publish_drops != null && [t('status.drops', 'Dropped'), this._stNum(mqtt.publish_drops), mqtt.publish_drops ? 'st-warn-t' : ''],
                [t('status.reconnects', 'Reconnects'), this._stNum(mqtt.connection_count || 0)],
                mqtt.last_contact_age_s != null && [t('status.lastContact', 'last'), this._stDur(mqtt.last_contact_age_s)],
                mqtt.disconnected_for_s && [t('status.downFor', 'down for'), this._stDur(mqtt.disconnected_for_s), 'st-bad'],
            ]) : offLine('mqtt', settings)}</div>`;

        const inf = `<div class="st-card" data-key="influx">${head('influx', 'bi-database', 'InfluxDB', influx.enabled, influx.connected && !influx.auth_failed,
                influx.auth_failed ? t('status.why.authFailed', 'the token is refused') : influx.connected ? t('status.connected', 'connected') : t('status.disconnected', 'disconnected'),
                this._esc(influx.bucket || ''))}
            ${influx.enabled ? kv([
                [t('status.confirmed', 'Confirmed'), `${this._stNum(influx.writes_confirmed ?? influx.writes_total)} <span class="st-faint">${this._stNum(s.influxRate, 1)}/s</span>`],
                influx.last_confirm_age_s != null && [t('status.lastConfirm', 'last confirmed'), this._stDur(influx.last_confirm_age_s)],
                [t('status.buffer', 'Buffer (queue)'), `${this._stNum(influx.buffer_points || 0)} <span class="st-faint">${t('status.window', 'window')} ${this._esc(influx.buffer_minutes ?? '?')}m</span>`, influx.buffer_points ? 'st-warn-t' : ''],
                [t('status.replayed', 'Replayed'), this._stNum(influx.replayed_total || 0)],
                [t('status.dropped', 'Dropped'), this._stNum(influx.dropped_total || 0), influx.dropped_total ? 'st-warn-t' : ''],
                [t('status.failedWrites', 'Failed'), this._stNum(influx.writes_failed || 0), influx.writes_failed ? 'st-bad' : ''],
            ]) : offLine('influxdb', settings)}</div>`;

        const opOn = op && !op.unavailable && op.enabled;
        const opc = `<div class="st-card" data-key="opcua">${head('opcua', 'bi-diagram-2', 'OPC UA', opOn, op && op.running,
                op && op.running ? t('status.word.running', 'running') : t('status.why.opcuaDown', 'enabled, not running'),
                this._esc(op && op.endpoint || ''))}${this._stLate('opcua')}
            ${opOn ? kv([
                [t('status.variables', 'Variables'), this._stNum(op.variables)],
                [t('status.tab.devices', 'Devices'), this._stNum(op.devices)],
                [t('status.commands', 'Commands'), this._stNum(op.commands)],
                [t('status.updates', 'Updates'), this._stNum(op.updates)],
                [t('status.encryption', 'Encryption'), this._esc(op.encryption || 'off')],
                op.error && [t('status.errors', 'Errors'), this._esc(op.error), 'st-bad'],
            ]) : op && op.unavailable ? `<div class="st-out-off">${t('status.unavailable', 'not available')}</div>` : offLine('opcua', settings)}</div>`;

        const kb = n => n == null ? '—' : n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';
        const vmRows = insts.map(i => {
            const peers = (Array.isArray(i.connections) ? i.connections : []).map(c => `${c.ip}:${c.port}`).join(', ') || (typeof i.peers === 'string' ? i.peers : '');
            const word = !i.enabled ? 'disabled' : i.waits_for_active ? 'standby' : i.running ? (i.state === 'ok' || !i.state ? 'ok' : 'stale') : 'stopped';
            const cls = word === 'stopped' ? 'error' : this._stWordCls(word);
            return `<tr data-key="${this._esc((i.name || i.template) + ':' + i.port)}">
                <td>${this._stDot(cls, this._stWord(word))}</td>
                <td><span class="st-name">${this._esc(i.name || i.template)}</span> <span class="st-chip">:${this._esc(i.port)}</span></td>
                <td class="num">${this._stNum(i.conn_count || 0)}${peers ? ` <span class="st-faint st-hide-sm">${this._esc(peers)}</span>` : ''}</td>
                <td class="num st-hide-sm">${this._stNum(i.req_rate, 1)}/s · ${this._stNum(i.requests)}</td>
                <td class="num${i.errors ? ' st-bad' : ' st-muted'}">${this._stNum(i.errors || 0)}</td>
                <td class="num st-muted st-hide-sm">↓${kb(i.bytes_rx)} ↑${kb(i.bytes_tx)}</td>
                <td class="num">${i.freshness_age_s != null ? this._stDur(i.freshness_age_s) : '—'}</td></tr>`;
        }).join('');
        const vm = `<div class="st-card" data-key="vm"><div class="st-card-title"><i aria-hidden="true" class="bi bi-hdd-network"></i> ${t('nav.vmeters', 'Virtual Meters')}${this._stLate('vm')}</div>
            ${insts.length ? `<div class="st-scroll"><table class="st-table"><thead><tr>
                <th scope="col">${t('status.health', 'Health')}</th><th scope="col">${t('status.meter', 'Meter')}</th><th scope="col">${t('status.clients', 'Clients')}</th>
                <th scope="col" class="st-hide-sm">${t('status.requests', 'Requests')}</th><th scope="col">${t('status.errors', 'Errors')}</th>
                <th scope="col" class="st-hide-sm">RX / TX</th><th scope="col">${t('status.fresh', 'Fresh')}</th></tr></thead><tbody>${vmRows}</tbody></table></div>`
              : offLine('vmeters', t('nav.vmeters', 'Virtual Meters'))}</div>`;
        return mq + inf + opc + vm;
    },

    // the alerts box: its Test button and result live in the static frame, so
    // a refresh never takes the spinner or the answer away
    _stAlerts(set) {
        const t = this.t.bind(this), A = this._st.data.alerts;
        const a = A && A.status;
        const btn = document.getElementById('statusTestAlertBtn');
        if (btn) btn.hidden = !(a && a.enabled && a.channels && a.channels.length);
        set('stAlertHead', `<i aria-hidden="true" class="bi bi-bell${a && a.enabled ? '-fill' : ''}"></i><span class="st-out-name">${t('status.alerts', 'Alerts')}</span>
            ${!a ? '' : this._stDot(a.enabled ? 'ok' : 'off', a.enabled ? t('status.armed', 'armed') : t('status.off', 'off'))}
            ${a && a.enabled ? `<span class="st-muted">${this._esc((a.channels || []).join(' · ') || t('status.noChannel', 'no channel'))}</span>` : ''}${this._stLate('alerts')}`);
        if (!a) { set('stAlertBody', ''); return; }
        const settingsBtn = `<button type="button" class="btn btn-ghost btn-sm" ${this._act('openSettingsTab', ['alerts'])}>${t('nav.config', 'Settings')} <i aria-hidden="true" class="bi bi-arrow-right"></i></button>`;
        if (!a.enabled) { set('stAlertBody', `<div class="st-out-off">${t('status.offHint', 'Not enabled.')} ${settingsBtn}</div>`); return; }
        const sig = a.signals || {}, th = a.thresholds || {};
        const SIGNALS = [['device', 'alerts.sigDevice', 'Device down / recovers'], ['sink', 'alerts.sigSink', 'Sink disconnect / reconnect'],
            ['latency', 'alerts.sigLatency', 'High read latency'], ['buffer', 'alerts.sigBuffer', 'InfluxDB buffer backlog'],
            ['alarm', 'alerts.sigAlarm', 'Device alarms (declared by its template)'], ['threshold', 'alerts.sigThreshold', 'Value thresholds (warning / danger)']];
        const list = (A && A.alerts) || [];
        this._stKeys = new Map();
        const kv = items => `<dl class="st-kv">${items.filter(Boolean).map(([k, v]) => `<div><dt>${this._esc(k)}</dt><dd>${v}</dd></div>`).join('')}</dl>`;
        set('stAlertBody', `${kv([
                [t('status.rateLimit', 'At most'), t('status.onePer', 'one per source every {d}', { d: this._stDur(a.min_interval_s) })],
                sig.latency && [t('status.latencyOver', 'Latency over'), `${this._stNum(th.latency_ms)} ms`],
                sig.buffer && [t('status.bufferOver', 'Buffer over'), `${this._stNum(th.buffer_points)} ${t('status.points', 'points')}`],
                sig.threshold && [t('status.deadband', 'Deadband'), `${this._stNum(a.threshold_deadband_pct, 1)} %`],
            ])}
            <div class="st-sigs" aria-label="${this._esc(t('alerts.signals', 'Signals'))}">${SIGNALS.map(([k, key, d]) =>
                `<span class="st-badge st-${sig[k] ? 'ok' : 'info'}" title="${this._esc(sig[k] ? t('status.sigOn', 'alerts are sent') : t('status.sigOff', 'not alerted'))}"><i aria-hidden="true" class="bi bi-${sig[k] ? 'check-lg' : 'dash'}"></i> ${this._esc(t(key, d))}</span>`).join('')}</div>
            <div class="st-subtitle">${t('status.lastAlerts', 'Last alerts sent')}</div>
            ${!list.length ? `<p class="st-muted" style="margin:0;">${t('status.noAlertsSent', 'No alert sent since the gateway started.')}</p>`
              : `<div class="st-scroll"><table class="st-table st-log"><tbody>${list.map(x => {
                const lv = StatusModel.normLevel(x.severity);
                return `<tr data-key="${this._esc(this._stKey(`${x.ts}:${x.source}:${x.message}`))}"><td class="num st-muted">${x.ts ? this._stTime(x.ts) : ''}</td>
                    <td><span class="st-badge st-${lv}">${this._esc(this.t('status.sev.' + lv, lv))}</span></td>
                    <td class="st-muted st-hide-sm">${this._esc(x.source || '')}</td><td>${this._esc(x.message || '')}</td></tr>`;
              }).join('')}</tbody></table></div>`}
            <div style="margin-top:8px;">${settingsBtn}</div>`);
    },

    // where each device's data goes: one row per device, one column per output
    _stRouting(set) {
        const t = this.t.bind(this), st = this._st.data.status || {};
        const devs = (st.devices || []).filter(d => d.enabled !== false);
        if (!devs.length) { set('stRouting', ''); return; }
        const mqttOn = !!(st.mqtt || {}).enabled, influxOn = !!(st.influxdb || {}).enabled;
        const op = this._st.data.opcua, opOn = !!(op && !op.unavailable && op.enabled);
        const cols = [
            ['mqtt_enabled', 'MQTT', mqttOn], ['influxdb_enabled', 'InfluxDB', influxOn], ['opcua_enabled', 'OPC UA', opOn],
            ['http_output_enabled', 'HTTP', true], ['rest_push_enabled', 'REST push', true], ['ha_discovery_enabled', 'Home Assistant', mqttOn],
        ];
        const yes = `<i aria-hidden="true" class="bi bi-check-lg st-yes"></i><span class="sr-only">${this._esc(t('status.yes', 'yes'))}</span>`;
        const no = `<span class="st-faint" aria-hidden="true">·</span><span class="sr-only">${this._esc(t('status.no', 'no'))}</span>`;
        const rows = devs.slice().sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id))).map(d => `<tr data-key="${this._esc(d.id)}">
            <td><span class="st-name">${this._esc(d.name || d.id)}</span></td>
            ${cols.map(([f, , on]) => `<td class="st-c${on ? '' : ' st-dim'}">${d[f] ? yes : no}</td>`).join('')}</tr>`).join('');
        set('stRouting', `<details class="st-card" id="stRoutingBox">
            <summary class="st-card-title"><i aria-hidden="true" class="bi bi-signpost-split"></i> ${t('status.routing', "Where each device's data goes")}
                <span class="st-faint">${t('status.routingHint', 'per device; a dimmed column is an output that is off')}</span></summary>
            <div class="st-scroll"><table class="st-table st-matrix"><thead><tr><th scope="col">${t('status.device', 'Device')}</th>${cols.map(([, label, on]) =>
                `<th scope="col" class="st-c${on ? '' : ' st-dim'}">${this._esc(label)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>
        </details>`);
    },

    _stAutomation(m) {
        const t = this.t.bind(this), D = this._st.data;
        const rules = m.rules || [];
        const ruleRows = rules.map(r => {
            const live = r.live || {}, last = live.last || {};
            const cls = r.sev === 'error' ? 'error' : r.sev === 'warn' ? 'warn' : r.enabled === false ? 'off' : m.standby ? 'info' : 'ok';
            const lastTxt = last.ts ? `${this._stTime(last.ts)} · ${this._esc(last.action || '')}${last.result ? ' → ' + this._esc(last.result) : ''}` : '—';
            const word = this._stWord(r.word);
            return `<tr data-key="${this._esc(r.id)}" tabindex="0" data-key-enter ${this._act('openRule', [r.id])} aria-label="${this._esc(`${r.label || r.id}: ${word}`)}">
                <td>${this._stDot(cls, word)}</td>
                <td><span class="st-name">${this._esc(r.label || r.id)}</span>${r.label ? ` <span class="st-faint">${this._esc(r.id)}</span>` : ''}</td>
                <td class="st-muted st-hide-sm">${this._esc(r.kind || '')}</td>
                <td><span class="st-badge st-${r.armed ? 'ok' : 'info'}">${r.armed ? t('status.armed', 'armed') : t('status.shadow', 'shadow')}</span></td>
                <td class="num st-muted st-hide-sm">${lastTxt}</td>
                <td class="st-bad">${this._esc(live.error ? String(live.error) : '')}</td></tr>`;
        }).join('');
        const rulesCard = `<div class="st-card" data-key="rules"><div class="st-card-title"><i aria-hidden="true" class="bi bi-sliders"></i> ${t('nav.rules', 'Rules')}${this._stLate('rules')}</div>
            ${rules.length ? `<div class="st-scroll"><table class="st-table"><thead><tr><th scope="col">${t('status.state', 'State')}</th><th scope="col">${t('status.rule', 'Rule')}</th>
                <th scope="col" class="st-hide-sm">${t('status.kind', 'Kind')}</th><th scope="col">${t('status.mode', 'Mode')}</th>
                <th scope="col" class="st-hide-sm">${t('status.lastDecision', 'Last decision')}</th><th scope="col">${t('status.errors', 'Errors')}</th></tr></thead><tbody>${ruleRows}</tbody></table></div>`
              : `<p class="st-muted" style="margin:0;">${t('status.noRules', 'No rules.')}</p>`}</div>`;
        const seqs = (D.sequences && D.sequences.sequences) || [];
        const seqCls = { done: 'ok', running: 'warn', undoing: 'warn', cancelled: 'off', dry_run: 'off', aborted: 'error' };
        const seqCard = `<div class="st-card" data-key="seq"><div class="st-card-title"><i aria-hidden="true" class="bi bi-list-ol"></i> ${t('status.sequences', 'Sequences')}${this._stLate('sequences')}</div>
            ${seqs.length ? `<table class="st-table"><tbody>${seqs.map(q => {
                const run = q.running || (q.last || [])[0];
                return `<tr data-key="${this._esc(q.id)}" tabindex="0" data-key-enter ${this._act('navigateTo', ['rules'])}>
                    <td>${run ? this._stDot(seqCls[run.status] || 'off', this._stWord(run.status)) : this._stDot('off', t('status.neverRun', 'never run'))}</td>
                    <td><span class="st-name">${this._esc(q.label || q.id)}</span></td>
                    <td class="num st-muted st-hide-sm">${run && run.started ? this._stTime(run.started) : ''}</td>
                    <td class="st-muted">${this._esc(run && run.reason || '')}</td></tr>`;
            }).join('')}</tbody></table>` : `<p class="st-muted" style="margin:0;">${t('status.noSequences', 'No sequences.')}</p>`}</div>`;
        return rulesCard + seqCard;
    },

    _stSpark(values) {
        const t = this.t.bind(this);
        if (!values || values.length < 2) return `<span class="st-faint"><i aria-hidden="true" class="bi bi-hourglass-split"></i> ${t('status.collecting', 'collecting data…')}</span>`;
        const max = Math.max(...values, 0);
        const pts = values.map((v, i) => `${(i / (values.length - 1) * 100).toFixed(2)},${max > 0 ? (38 - v / max * 34).toFixed(2) : 38}`).join(' ');
        return `<svg class="st-spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="${this._esc(t('status.readsTrend', 'reads per second, last 3 minutes'))}">
            <line class="st-axis" x1="0" y1="39" x2="100" y2="39"></line><polyline points="${pts}"></polyline></svg>
            ${max > 0 ? `<span class="st-faint">${t('status.peak', 'peak')} ${this._stNum(max, 1)}/s</span>` : `<span class="st-faint">${t('status.idleNow', 'idle — nothing read')}</span>`}`;
    },

    _stGateway(m) {
        const t = this.t.bind(this), s = this._st, D = s.data, st = D.status || {}, res = D.res || {}, role = m.role || {};
        const cfg = st.config || {};
        const kv = items => `<dl class="st-kv">${items.filter(Boolean).map(([k, v, cls]) =>
            `<div><dt>${this._esc(k)}</dt><dd${cls ? ` class="${cls}"` : ''}>${v}</dd></div>`).join('')}</dl>`;
        const roleCard = `<div class="st-card" data-key="role"><div class="st-card-title"><i aria-hidden="true" class="bi bi-shield-check"></i> ${t('status.roleTitle', 'Role and configuration')}</div>
            ${kv([
                [t('status.role', 'Role'), role.role ? `<span class="st-role${role.role === 'standby' ? ' st-role-standby' : ''}">${this._esc(t('status.role.' + role.role, role.role).toUpperCase())}</span>` : '—'],
                role.since && [t('status.since', 'since'), this._esc(this._stTime(role.since))],
                role.set_by && [t('status.setBy', 'set by'), this._esc(role.set_by)],
                role.reason && [t('status.reason', 'why'), this._esc(role.reason)],
                [t('status.config', 'Configuration'), cfg.saves_disabled ? t('status.why.savesDisabled', 'the configuration did not load — saving is refused')
                    : cfg.healed_from_snapshot ? t('status.why.healed', 'restored from a snapshot at start')
                    : cfg.written_by_newer ? t('status.why.newer', 'written by a newer gateway version') : t('status.healthy', 'healthy'),
                    cfg.saves_disabled ? 'st-bad' : cfg.healed_from_snapshot || cfg.written_by_newer ? 'st-warn-t' : ''],
            ])}</div>`;
        const resCard = `<div class="st-card" data-key="res"><div class="st-card-title"><i aria-hidden="true" class="bi bi-cpu"></i> ${t('status.resources', 'Resources')}${this._stLate('res')}</div>
            ${kv([
                ['CPU', res.cpu_pct != null ? `${this._stNum(res.cpu_pct, 1)} %` : '—', res.cpu_pct > 80 ? 'st-bad' : res.cpu_pct > 50 ? 'st-warn-t' : ''],
                [t('status.memory', 'Memory'), res.rss_mb != null ? `${this._stNum(res.rss_mb)} MB` : '—'],
                [t('status.threads', 'Threads'), this._stNum(res.threads)],
                [t('status.openFds', 'Open FDs'), this._stNum(res.open_fds)],
                [t('status.connections', 'TCP conns'), this._stNum(res.tcp_established)],
                [t('status.uiClients', 'UI clients'), this._stNum(st.websocket_clients)],
                [t('status.uptime', 'Uptime'), this._stDur(res.uptime_s)],
            ])}</div>`;
        const reads = s.hist.length ? s.hist[s.hist.length - 1] : null;
        const flow = `<div class="st-card" data-key="flow"><div class="st-card-title"><i aria-hidden="true" class="bi bi-activity"></i> ${t('status.throughput', 'Throughput')}</div>
            ${kv([
                [t('status.reads', 'Reads'), reads == null ? '—' : `${this._stNum(reads, 1)}/s`],
                ['MQTT', `${this._stNum(s.mqttRate, 1)}/s`],
                ['InfluxDB', `${this._stNum(s.influxRate, 1)}/s`],
            ])}<div style="margin-top:8px;">${this._stSpark(s.hist)}</div></div>`;
        const bus = D.bus || {};
        const polled = bus.polled || [], listened = bus.listened || [];
        const busCard = (polled.length || listened.length) ? `<div class="st-card" data-key="bus"><div class="st-card-title"><i aria-hidden="true" class="bi bi-ethernet"></i> ${t('status.buses', 'Buses')}${this._stLate('bus')}</div>
            <div class="st-scroll"><table class="st-table"><tbody>
            ${polled.map(b => `<tr data-key="p:${this._esc(b.bus)}"><td>${this._stDot(b.answering ? 'ok' : 'error', b.answering ? t('status.answering', 'answering') : t('status.silent', 'silent'))}</td>
                <td><span class="st-name">${this._esc(b.bus)}</span></td><td class="st-muted">${t('status.polled', 'polled')} · ${this._esc((b.devices || []).length ?? b.devices)} ${t('status.tab.devices', 'Devices').toLowerCase()}</td></tr>`).join('')}
            ${listened.map(b => `<tr data-key="l:${this._esc(b.port)}"><td>${this._stDot(b.open_error ? 'error' : b.last_frame_age_s != null && b.last_frame_age_s < 60 ? 'ok' : 'warn', b.open_error ? t('status.word.down', 'down') : t('status.listening', 'listening'))}</td>
                <td><span class="st-name">${this._esc(b.port)}</span></td><td class="st-muted">${this._stNum(b.frames)} ${t('status.frames', 'frames')} · ${t('status.lastFrame', 'last')} ${b.last_frame_age_s != null ? this._stDur(b.last_frame_age_s) : '—'}${b.open_error ? ' · ' + this._esc(b.open_error) : ''}</td></tr>`).join('')}
            </tbody></table></div></div>` : '';
        const bridges = m.bridges || [];
        const brCard = bridges.length ? `<div class="st-card" data-key="bridges"><div class="st-card-title"><i aria-hidden="true" class="bi bi-hdd-network"></i> ${t('status.bridges', 'Bridges')}${this._stLate('bridges')}</div>
            <div class="st-scroll"><table class="st-table"><tbody>${bridges.map(b => {
                const stt = (b.state || {}).status || 'unknown';
                const cls = b.quiet && stt === 'offline' ? 'off' : stt === 'online' ? 'ok' : stt === 'degraded' ? 'warn' : stt === 'offline' ? 'error' : 'off';
                const word = b.quiet && stt === 'offline' ? t('status.word.asleep', 'asleep') : t('bridges.state.' + stt, stt);
                const busy = (b.ports || []).filter(p => p.busy).map(p => `${this._esc(p.label || p.port)} ${p.busy.over_capacity
                    ? this._esc(this.t('bridges.overShort', 'over capacity')) : `${this._stNum(p.busy.pct)}%`}`).join(' · ');
                return `<tr data-key="${this._esc(b.id)}" tabindex="0" data-key-enter ${this._act('openBridge', [b.id])}>
                    <td>${this._stDot(cls, word)}</td><td><span class="st-name">${this._esc(b.name || b.id)}</span> <span class="st-chip">${this._esc(b.type_name || b.type || '')}</span></td>
                    <td class="st-muted st-hide-sm">${this._stNum((b.ports || []).length)} ${t('status.ports', 'ports')} · ${this._stNum(b.devices)} ${t('status.tab.devices', 'Devices').toLowerCase()}</td>
                    <td class="st-muted">${busy}</td></tr>`;
            }).join('')}</tbody></table></div></div>` : '';
        return roleCard + resCard + flow + busCard + brCard;
    },

    // the source picker lists the sources seen; rebuilt only when they change
    _stEvSourceOptions(force) {
        const s = this._st, sel = document.getElementById('stEvSource');
        const E = s.data.events;
        if (!sel) return;
        const srcs = [...new Set(((E && E.events) || []).map(e => e.source || '').filter(Boolean))];
        if (s.evSource && !srcs.includes(s.evSource)) srcs.push(s.evSource);
        srcs.sort((a, b) => a.localeCompare(b));
        const sig = srcs.join('\n');
        if (!force && sig === s.evSources) return;
        s.evSources = sig;
        sel.innerHTML = `<option value="">${this.t('status.allSources', 'All sources')}</option>`
            + srcs.map(x => `<option value="${this._esc(x)}">${this._esc(x)}</option>`).join('');
        sel.value = s.evSource || '';
    },

    // a time today, a date and time before
    _stWhen(ts) {
        const d = new Date(ts * 1000), now = new Date();
        if (d.toDateString() === now.toDateString()) return this._stTime(ts);
        try { return d.toLocaleString(this._loc(), { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
        catch (e) { return d.toLocaleString(this._loc()); }
    },

    _stEvents() {
        const t = this.t.bind(this), s = this._st, E = s.data.events;
        this._stEvSourceOptions(false);
        const all = ((E && E.events) || []).map(e => ({ ...e, lv: StatusModel.normLevel(e.level) }))
            .sort((a, b) => (b.ts || 0) - (a.ts || 0));
        const q = (s.evSearch || '').trim().toLowerCase();
        // the level counts follow the search and the source, not the level itself
        const scoped = all.filter(e => (!s.evSource || e.source === s.evSource)
            && (!q || `${e.source || ''} ${e.message || ''}`.toLowerCase().includes(q)));
        const n = { all: scoped.length, error: 0, warn: 0, info: 0 };
        scoped.forEach(e => { n[e.lv]++; });
        for (const k of Object.keys(n)) {
            const el = document.getElementById('stEvN-' + k);
            if (el && el.textContent !== String(n[k])) el.textContent = String(n[k]);
        }
        const rows = scoped.filter(e => s.evFilter === 'all' || e.lv === s.evFilter);
        const cnt = document.getElementById('stEvCount');
        if (cnt) cnt.textContent = t('status.nOfM', '{n} of {m}', { n: rows.length, m: all.length });
        if (!E) return `<p class="st-muted st-pad">${t('status.unavailable', 'not available')}${this._stLate('events')}</p>`;
        if (!all.length) return `<p class="st-muted st-pad">${t('status.noEventsAtAll', 'No events recorded yet.')}</p>`;
        if (!rows.length) return `<p class="st-muted st-pad">${t('status.noMatch', 'Nothing matches.')}</p>`;
        this._stKeys = new Map();
        return `${this._stLate('events')}<table class="st-table st-log"><thead><tr>
            <th scope="col">${t('status.time', 'Time')}</th><th scope="col">${t('status.level', 'Level')}</th>
            <th scope="col" class="st-hide-sm">${t('status.source', 'Source')}</th><th scope="col">${t('status.message', 'Message')}</th></tr></thead>
            <tbody>${rows.map(e => `<tr data-key="${this._esc(this._stKey(`${e.ts}:${e.source}:${e.message}`))}">
            <td class="num st-muted">${e.ts ? this._esc(this._stWhen(e.ts)) : ''}</td>
            <td><span class="st-badge st-${e.lv}">${this._esc(t('status.sev.' + e.lv, e.lv))}</span></td>
            <td class="st-hide-sm">${e.source ? `<button type="button" class="st-linkish" ${this._act('statusEvSource', [e.source])} title="${this._esc(t('status.filterSource', 'Show only this source'))}">${this._esc(e.source)}</button>` : ''}</td>
            <td>${this._esc(e.message || '')}${e.source ? `<div class="st-faint st-show-sm">${this._esc(e.source)}</div>` : ''}</td></tr>`).join('')}</tbody></table>`;
    },

    // ── morphing: change only what differs, so an unchanged row is the same
    // element — its focus, hover and tooltip survive the 2 s refresh ──
    // a stable key for a row of a list newest-first: content, not position,
    // with a suffix only when two rows say the same thing
    _stKey(base) {
        const n = this._stKeys.get(base) || 0;
        this._stKeys.set(base, n + 1);
        return n ? `${base}#${n}` : base;
    },

    _morph(el, html) {
        if (el._stHtml === html) return;
        el._stHtml = html;
        const tpl = document.createElement('template');
        tpl.innerHTML = html;
        // a keyed row that moves is detached for a moment, which blurs it
        const focused = el.contains(document.activeElement) ? document.activeElement : null;
        this._morphChildren(el, tpl.content);
        if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    },
    _morphChildren(from, to) {
        const keyOf = n => (n.nodeType === 1 ? n.getAttribute('data-key') : null);
        const keyed = new Map();
        for (const n of from.childNodes) { const k = keyOf(n); if (k != null) keyed.set(k, n); }
        const want = Array.from(to.childNodes);
        want.forEach((nn, idx) => {
            const cur = from.childNodes[idx] || null;
            const k = keyOf(nn);
            let match = null;
            if (k != null) { match = keyed.get(k) || null; keyed.delete(k); }
            else if (cur && keyOf(cur) == null && cur.nodeType === nn.nodeType && (cur.nodeType !== 1 || cur.tagName === nn.tagName)) match = cur;
            if (match) {
                if (match !== cur) from.insertBefore(match, cur);
                this._morphNode(match, nn);
            } else {
                from.insertBefore(document.importNode(nn, true), cur);
            }
        });
        while (from.childNodes.length > want.length) from.removeChild(from.lastChild);
    },
    _morphNode(a, b) {
        if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
        for (const { name } of Array.from(a.attributes)) {
            if (name === 'open' && a.tagName === 'DETAILS') continue;      // the user's own choice
            if (!b.hasAttribute(name)) a.removeAttribute(name);
        }
        for (const { name, value } of Array.from(b.attributes)) if (a.getAttribute(name) !== value) a.setAttribute(name, value);
        this._morphChildren(a, b);
    },

    // ── open a rule or a bridge from here: navigate, then find, scroll to and
    // highlight it once its page has drawn it (and again if it redraws) ──
    openRule(id) {
        this._pendingFocus = { sel: `[data-rule="${CSS.escape(String(id))}"]`, until: Date.now() + 60000, done: false };
        this.navigateTo('rules');
        this._applyPendingFocus();
    },
    openBridge(id) {
        this._pendingFocus = { sel: `[data-bridge="${CSS.escape(String(id))}"]`, until: Date.now() + 60000, done: false };
        this.navigateTo('devices', { fromNav: true });
        this._applyPendingFocus();
    },
    // called by the Rules page after each draw, and polled briefly here; the
    // highlight is kept for a minute, through the page's own redraws
    _applyPendingFocus(tries = 0) {
        const p = this._pendingFocus;
        if (!p) return;
        if (Date.now() > p.until) { this._pendingFocus = null; return; }
        if (tries === 0 && p.polling) { this._pendingFocusOnce(p); return; }
        p.polling = true;
        if (this._pendingFocusOnce(p)) { p.polling = false; return; }
        if (tries < 40) setTimeout(() => this._applyPendingFocus(tries + 1), 150);
        else p.polling = false;
    },
    _pendingFocusOnce(p) {
        const el = document.querySelector(p.sel);
        if (el) {
            el.classList.add('st-highlight');
            if (!p.done) {
                p.done = true;
                if (!el.hasAttribute('tabindex')) el.tabIndex = -1;
                el.scrollIntoView({ block: 'center' });
                el.focus({ preventScroll: true });
            }
        }
        return !!el;
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
        const _t = this.t.bind(this);
        const _state = `${_t('status.vmeters', 'Virtual meters')}: ${online} ok`
            + (stale ? `, ${stale} ${_t('status.stale', 'stale')}` : '')
            + (down ? `, ${down} ${_t('status.down', 'down')}` : '') + ` / ${total}`;
        pill.title = _state + ' — ' + _t('status.clickToOpen', 'click to open');
        // state in the accessible name too, like its sibling pills (the dot is
        // color-only otherwise)
        pill.setAttribute('aria-label', _state);
    },

    // a header pill's state in words, for its accessible name
    _linkWord(s) {
        return !s.enabled ? this.t('status.linkOff', 'disabled')
            : s.connected ? this.t('status.linkUp', 'connected') : this.t('status.linkDown', 'disconnected');
    },

    // A tab left open across a deploy runs the old page and saves in the old
    // shape (FINDINGS PROD-5/6): when the gateway serves another build than
    // the one this page loaded, say so — never reload by itself, an edit in
    // progress would be lost.
    _checkBuild(serverBuild) {
        const el = document.getElementById('buildBanner');
        const mine = document.querySelector('meta[name="mbg-build"]')?.content || '';
        if (!el) return;
        const stale = !!(mine && serverBuild && serverBuild !== mine);
        if (!stale || this._buildLater === serverBuild) { el.hidden = true; return; }
        if (!el.hidden && el.dataset.build === serverBuild) return;
        const t = (k, d) => this._esc(this.t(k, d));
        el.dataset.build = serverBuild;
        el.innerHTML = `<i class="bi bi-arrow-repeat" aria-hidden="true"></i>
            <strong>${t('build.updated', 'The gateway was updated.')}</strong>
            <span>${t('build.reloadText', 'Reload to use the new version. Save what you are editing first — a reload loses it.')}</span>
            <span class="build-banner-actions">
                <button type="button" class="btn btn-primary btn-sm" ${this._act('buildBannerReload')}>${t('build.reload', 'Reload')}</button>
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('buildBannerLater', [serverBuild])}>${t('build.later', 'Later')}</button>
            </span>`;
        el.hidden = false;
    },

    buildBannerReload() { location.reload(); },

    // until the next build: a later deploy asks again
    buildBannerLater(build) {
        this._buildLater = build;
        const el = document.getElementById('buildBanner');
        if (el) el.hidden = true;
        document.querySelector('.page.active')?.focus();
    },

    async loadStatus() {
        try {
            const response = await fetch('/api/status');
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const status = await response.json();

            const verEl = document.getElementById('appVersion');
            if (verEl && status.version) verEl.textContent = 'v' + status.version;
            this._checkBuild(status.ui_build);

            const modbus = status.modbus || {};
            const mqtt = status.mqtt || {};
            const influx = status.influxdb || {};

            this._maybeFirstRun(modbus, status.devices);
            if (this._devFindLivePaint) this._devFindLivePaint(status.devices);

            // Update status indicators in titlebar
            const statusDevices = document.getElementById('statusDevices');
            const statusMqtt = document.getElementById('statusMqtt');
            const statusInflux = document.getElementById('statusInflux');

            if (statusDevices) {
                // Online devices out of the enabled ones (the Modbus primary is
                // just one of them now). Green = all up, amber = some down, red = none.
                // the Status page's own count (status-model.js): answering or
                // asleep, out of the enabled ones — never two numbers on one screen
                const standby = this._st?.data?.health?.role?.role === 'standby' || document.body.classList.contains('role-standby');
                const { online, total } = window.StatusModel
                    ? StatusModel.onlineCount(status.devices || [], standby)
                    : { online: (status.devices || []).filter(d => d.enabled && d.connected).length,
                        total: (status.devices || []).filter(d => d.enabled).length };
                const cnt = document.getElementById('devicesOnlineCount');
                if (cnt) cnt.textContent = `${online}/${total}`;
                statusDevices.classList.toggle('connected', total > 0 && online === total);
                statusDevices.classList.toggle('partial', online > 0 && online < total);
                statusDevices.classList.toggle('disconnected', total > 0 && online === 0);
                statusDevices.setAttribute('aria-label', this.t('status.devicesAria',
                    'Devices: {online} of {total} online', { online, total }));
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
                // the dot alone is color-only (1.4.1) — keep the accessible
                // name carrying the STATE, not just the service name
                statusMqtt.setAttribute('aria-label', 'MQTT: ' + this._linkWord(mqtt));
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
                statusInflux.setAttribute('aria-label', 'InfluxDB: ' + this._linkWord(influx));
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
                statMqttMsg.textContent = (mqtt.messages_published ?? null) === null ? '--' : (mqtt.messages_published).toLocaleString(this._loc());
            }

            const statInfluxPts = document.getElementById('statInfluxPts');
            if (statInfluxPts) {
                statInfluxPts.textContent = (influx.writes_total ?? null) === null ? '--' : (influx.writes_total).toLocaleString(this._loc());
            }

            // Store for status page
            this.status = status;

            // Recovered: tell the user once (the heartbeat is our liveness probe).
            if (this._statusLost) {
                this._statusLost = false;
                this.showToast?.('success', this.t('conn.reconnected', 'Reconnected'),
                                 this.t('conn.reconnectedDesc', 'The gateway is responding again.'));
            }
        } catch (error) {
            console.error('Failed to load status:', error);
            // Surface a lost connection ONCE (not every 5s poll) so a blank/stale
            // UI isn't silent — otherwise a boot-time or mid-session backend
            // outage leaves the operator guessing.
            if (!this._statusLost) {
                this._statusLost = true;
                this.showToast?.('error', this.t('conn.lostTitle', 'Connection lost'),
                                 this.t('conn.lostDesc', 'Cannot reach the gateway — retrying every 5s…'));
            }
        }
    },

    updatePollGroupsStatus() {
        // Render the status-bar poll-group intervals from the live config
        // (this.pollGroups) instead of a hardcoded label — keeps the bar in
        // sync with config/selected_registers.json (e.g. realtime 250ms).
        const el = document.getElementById('pollGroupsStatus');
        if (!el) return;
        const fmt = (s) => (s < 1 ? `${Math.round(s * 1000)}ms` : `${s}s`);
        if (this._footerOverride && this.currentPage === 'devices'
            && document.getElementById('deviceDetailView')?.style.display !== 'none') {
            // a unit page is open: its own rhythm, per source, not the primary's
            el.innerHTML = `<span class="poll-item"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this.t('devices.readEvery', 'read every')} ${
                this._footerOverride.map(r => `${r.interval_s != null ? fmt(r.interval_s) : '—'} (${this._esc(r.id)})`).join(' · ')}</span>`;
            return;
        }
        if (!this.pollGroups) return;
        const icons = { realtime: 'lightning-fill', normal: 'clock', slow: 'hourglass' };
        const items = Object.entries(this.pollGroups);
        if (!items.length) return;
        el.innerHTML = items.map(([name, g]) =>
            `<span class="poll-item"><i aria-hidden="true" class="bi bi-${icons[name] || 'clock'}"></i> ${this._esc(this.t('pg.' + name, name))}: ${fmt(g.interval)}</span>`
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
            title = '<i aria-hidden="true" class="bi bi-hdd-network"></i> Modbus Status';
            html += `
                <div class="status-detail-row">
                    <span class="status-detail-label">${this.t('lbl.status', "Status")}</span>
                    <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                        ${data.connected ? this.t('status.connected', 'connected') : this.t('status.disconnected', 'disconnected')}
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
                    <span class="status-detail-value mono">${data.last_success_ts ? new Date(data.last_success_ts * 1000).toLocaleString(this._loc()) : '-'}</span>
                </div>
                ${(data.poll_groups_detail || []).map(g => `
                <div class="status-detail-row">
                    <span class="status-detail-label">↳ ${this._esc(g.name)} (${this._fmtInterval(g.interval)})</span>
                    <span class="status-detail-value mono">${g.age_s != null ? g.age_s + 's' : '-'} · ${g.poll_count} polls</span>
                </div>`).join('')}
                ${(data.events && data.events.length) ? `<div class="status-detail-row"><span class="status-detail-label" style="color:var(--text-secondary);">${this.t('lbl.recentFailures', "Recent read failures")}</span><span class="status-detail-value">${data.events.length}</span></div>` : ''}
                ${(data.events || []).slice(-6).reverse().map(e => `
                <div class="status-detail-row">
                    <span class="status-detail-label" style="color:#c77700;"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(e.kind || '')}</span>
                    <span class="status-detail-value mono" style="font-size:11px;" title="${this._esc(e.message || '')}">${new Date(e.ts * 1000).toLocaleTimeString(this._loc())}</span>
                </div>`).join('')}
            `;
        } else if (service === 'mqtt') {
            const data = this.status.mqtt || {};
            title = '<i aria-hidden="true" class="bi bi-broadcast"></i> MQTT Status';
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
                            ${data.connected ? this.t('status.connected', 'connected') : this.t('status.disconnected', 'disconnected')}
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
                        <i aria-hidden="true" class="bi bi-info-circle"></i>
                        Skipped = unchanged values (publish mode: ${this._esc(data.publish_mode || 'changed')})
                    </div>
                    ` : ''}
                `;
            }
        } else if (service === 'influxdb') {
            const data = this.status.influxdb || {};
            title = '<i aria-hidden="true" class="bi bi-database"></i> InfluxDB Status';
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
                            ${data.connected ? this.t('status.connected', 'connected') : this.t('status.disconnected', 'disconnected')}
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
                        <i aria-hidden="true" class="bi bi-info-circle"></i>
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
        // the result span sits right after the button — resolve it relatively:
        // the Settings panel and the Status banner each have their own (they
        // used to share one id, so the Settings "Test" wrote into the hidden
        // status-page span and appeared dead)
        const out = (btn && btn.nextElementSibling && btn.nextElementSibling.tagName === 'SPAN')
            ? btn.nextElementSibling : document.getElementById('alertTestResult');
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

});
