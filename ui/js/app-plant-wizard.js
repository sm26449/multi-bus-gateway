// Plant wizard — adding an INSTALLATION, in the order an operator knows the answers.
//
// 1. the installation: a name, how the datalogger is reached, its address,
//    where it publishes — and a Test that checks exactly what was ticked;
// 2. what it holds — asked of the datalogger (Solar API) and shown found,
//    with a manual fallback; templates follow from role × protocol;
// 3. how often, as labelled numbers with the measured floor beside them;
// 4. review, down to device ids and topics, then create.
Object.assign(JanitzaMonitor.prototype, {

    ROLE_PRESETS: {
        inverter: { icon: 'bi-sun', label: 'Inverters', group: 'inverters',
                    hint: 'The units that generate. Their powers add up.' },
        site:     { icon: 'bi-house', label: 'Site totals', group: 'site',
                    hint: 'The balance the datalogger computes: generation, load, grid, autonomy.' },
        meter:    { icon: 'bi-speedometer2', label: 'Grid meter', group: 'grid',
                    hint: 'A meter at a measuring point — grid, consumption, a sub-circuit. Totalled on its own, never added to generation.' },
        battery:  { icon: 'bi-battery-half', label: 'Battery', group: 'battery',
                    hint: 'Storage units.' },
        sensor:   { icon: 'bi-thermometer-half', label: 'Sensors', group: 'sensors',
                    hint: 'Temperature, humidity, irradiance…' },
    },

    // the exact Solar API calls — typed by hand, one wrong parameter still
    // answers 200 OK with an empty body
    _SOLAR_API: {
        inverter: '/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData',
        site: '/solar_api/v1/GetPowerFlowRealtimeData.fcgi',
        meter: '/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}',
    },
    _TPL_FOR: {           // role × transport → the bundled template, when there is one
        'inverter:http': 'fronius_solar_api_inverter', 'inverter:modbus': 'fronius_sunspec_inverter',
        'site:http': 'fronius_solar_api_site',
        'meter:http': 'fronius_solar_api', 'meter:modbus': 'fronius_sunspec_meter',
    },

    async openPlantWizard() {
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        this._plantWiz = {
            step: 1, templates,
            data: {
                id: '', idTouched: false, name: '', host: '', port: 502, ways: 'both', root: 'pv',
                probe: {}, discovered: null, groups: [],
            },
        };
        document.getElementById('plantWizTitle').textContent =
            this.t('plant.wizard.title', 'Add Installation');
        this._plantWizRender();
        this.openModal('plantWizardModal');
        document.querySelectorAll('#plantWizSteps .wizard-step').forEach(li => {
            li.tabIndex = 0;
            li.onclick = () => {
                const n = parseInt(li.dataset.step, 10);
                if (n < this._plantWiz.step) {
                    this._plantWizCollect(); this._plantWiz.step = n; this._plantWizRender();
                }
            };
        });
    },

    _plantWizRender() {
        const w = this._plantWiz;
        document.querySelectorAll('#plantWizSteps .wizard-step').forEach(s => {
            const n = parseInt(s.dataset.step, 10);
            s.classList.toggle('active', n === w.step);
            s.classList.toggle('done', n < w.step);
            if (n === w.step) s.setAttribute('aria-current', 'step');
            else s.removeAttribute('aria-current');
        });
        document.getElementById('plantWizBack').style.visibility = w.step === 1 ? 'hidden' : 'visible';
        document.getElementById('plantWizNextLabel').textContent = w.step === 4
            ? this.t('plant.wizard.create', 'Create installation') : this.t('common.next', 'Next');
        document.getElementById('plantWizFeedback').textContent = '';
        const body = document.getElementById('plantWizBody');
        body.innerHTML = w.step === 1 ? this._plantWizStep1()
                       : w.step === 2 ? this._plantWizStep2()
                       : w.step === 3 ? this._plantWizStep3()
                       : this._plantWizStep4();
        const first = body.querySelector('input:not([type=checkbox]):not([type=radio]), select');
        if (first) setTimeout(() => first.focus(), 30);
    },

    _pwWays() { const d = this._plantWiz.data; return { http: d.ways !== 'modbus', modbus: d.ways !== 'http' }; },
    _pwHostPort() {
        const m = /^(.*?)(?::(\d{1,5}))?$/.exec(this._plantWiz.data.host || '');
        return { host: m ? m[1] : '', port: m && m[2] ? parseInt(m[2], 10) : 80 };
    },
    _pwSlug(name) {
        return String(name || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
            .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
    },

    // ── 1. the installation ─────────────────────────────────────────────────
    _plantWizStep1() {
        const t = (k, d) => this.t(k, d), d = this._plantWiz.data, ways = this._pwWays();
        const seg = (v, label, hint) => `<label class="seg-btn ${d.ways === v ? 'on' : ''}"><input type="radio" name="pwWays" value="${v}" ${d.ways === v ? 'checked' : ''} onchange="app.plantWizCollectAndRender()"><span class="s"></span> ${label}${hint ? ` <span class="seg-reco">${hint}</span>` : ''}</label>`;
        const pr = d.probe || {};
        const line = (r, name) => r ? `<div><span class="status-dot" style="--dot:${r.ok ? 'var(--success,#22c55e)' : 'var(--danger,#ef4444)'}" aria-hidden="true"></span> <b>${name}</b> ${this._esc(r.msg || '')}</div>` : '';
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.intro',
            'An installation is one site behind one datalogger. Say how you reach it; what it holds is asked of the datalogger next.')}</p>
        <div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="pwName">${t('plant.wizard.name', 'Name')}</label>
                <input id="pwName" class="input" value="${this._esc(d.name)}" placeholder="Fronius PV" oninput="app.plantWizNameTyped(this.value)"></div>
            <div class="form-group"><label class="form-label" for="pwId">${t('plant.wizard.id', 'ID')}</label>
                <input id="pwId" class="input" value="${this._esc(d.id)}" placeholder="fronius" oninput="app._plantWiz.data.idTouched = true">
                <div class="field-hint">${t('plant.wizard.idHint', 'a-z 0-9 - _ · in topics and device names, fixed after creation')}</div></div>
        </div>
        <div class="wiz-eyebrow">${t('plant.wizard.waysQ', 'How do we reach the datalogger?')}</div>
        <div class="seg" role="radiogroup" aria-label="${t('plant.wizard.waysQ', 'How do we reach the datalogger?')}">
            ${seg('http', 'Solar API (HTTP)', '')}
            ${seg('modbus', 'Modbus TCP', '')}
            ${seg('both', t('plant.wizard.both', 'Both'), t('plant.wizard.reco', 'recommended'))}
        </div>
        <div class="field-hint" style="margin:-8px 0 12px;">${ways.http && ways.modbus
            ? t('plant.wizard.bothHint', 'Solar API answers in milliseconds with the essentials; Modbus (SunSpec) is slow but complete. Read both: the fast one owns the fields it offers, Modbus fills in the rest.')
            : ways.http ? t('plant.wizard.httpHint', 'Fast and gentle on the datalogger; power, voltages, currents, energy — no per-string or event data.')
            : t('plant.wizard.modbusHint', 'Complete (SunSpec), but this datalogger serves one client slowly — count on ~2 s per read.')}</div>
        <div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="pwHost">${t('plant.wizard.host', 'Datalogger address')}</label>
                <input id="pwHost" class="input" value="${this._esc(d.host)}" placeholder="192.168.1.50"></div>
            ${ways.modbus ? `<div class="form-group"><label class="form-label" for="pwPort">${t('plant.wizard.modbusPort', 'Modbus port')}</label>
                <input id="pwPort" class="input" type="number" min="1" max="65535" value="${d.port}">
                <div class="field-hint">1–65535 · ${t('common.default', 'default')} 502</div></div>` : ''}
            <div class="form-group"><label class="form-label" for="pwRoot">${t('plant.wizard.root', 'Publishes under')}</label>
                <input id="pwRoot" class="input" value="${this._esc(d.root)}" placeholder="pv">
                <div class="field-hint"><code>${this._esc(d.root || 'pv')}/inverters/1/…</code> · <code>${this._esc(d.root || 'pv')}/site/…</code></div></div>
            <div class="form-group" style="align-self:flex-end;">
                <button class="btn btn-secondary btn-sm" onclick="app.plantWizProbe()" id="pwProbeBtn">
                    <i aria-hidden="true" class="bi bi-activity"></i> ${t('plant.wizard.probe', 'Test')}</button></div>
        </div>
        <div id="pwProbeOut" role="status" aria-live="polite" style="margin-top:6px;font-size:12.5px;display:flex;flex-direction:column;gap:4px;">
            ${line(pr.http, 'Solar API')}${line(pr.modbus, 'Modbus')}</div>`;
    },

    plantWizNameTyped(v) {
        const d = this._plantWiz.data;
        d.name = v;
        if (!d.idTouched) { const id = document.getElementById('pwId'); if (id) id.value = this._pwSlug(v); }
    },

    // Test checks exactly what was ticked, and says so in words. The Solar API
    // check IS the discovery: one call tells us it answers and what it holds.
    async plantWizProbe(silent = false) {
        this._plantWizCollect();
        const d = this._plantWiz.data, ways = this._pwWays();
        const out = document.getElementById('pwProbeOut');
        if (!d.host) { if (out) out.innerHTML = `<span style="color:var(--danger,#ef4444);">${this.t('plant.wizard.needHost', 'Enter the datalogger address first.')}</span>`; return false; }
        if (out && !silent) out.innerHTML = this.t('common.loading', 'Loading…');
        d.probe = {};
        const jobs = [];
        if (ways.http) jobs.push((async () => {
            const t0 = performance.now();
            try {
                const hp = this._pwHostPort();
                const r = await fetch(`/api/fronius/discover?host=${encodeURIComponent(hp.host)}&port=${hp.port}`);
                const j = await r.json();
                const ms = Math.round(performance.now() - t0);
                if (!r.ok) { d.probe.http = { ok: false, msg: j.detail || r.statusText }; d.discovered = null; return; }
                d.discovered = j;
                d.probe.http = { ok: true, ms, msg: `${this.t('plant.wizard.answered', 'answered')} · ${ms} ms · ${(j.inverters || []).length} ${this.t('plant.role.inverter', 'inverters').toLowerCase()}${(j.meters || []).length ? ` · ${(j.meters || []).length} ${this.t('plant.wizard.meters', 'meter(s)')}` : ''}` };
            } catch (e) { d.probe.http = { ok: false, msg: e.message }; d.discovered = null; }
        })());
        if (ways.modbus) jobs.push((async () => {
            const t0 = performance.now();
            try {
                const r = await fetch('/api/devices/test', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ connection: { protocol: 'tcp', host: this._pwHostPort().host, port: d.port, unit_id: 1 } }) });
                const j = await r.json();
                const ok = j.ok || j.success;
                const m = /(\d+(?:\.\d+)?)\s*ms/.exec(j.message || '');
                d.probe.modbus = { ok, ms: m ? parseFloat(m[1]) : Math.round(performance.now() - t0),
                                   msg: ok ? `${this.t('plant.wizard.answered', 'answered')} · ${m ? m[1] : Math.round(performance.now() - t0)} ms (unit 1)` : (j.error || j.message || this.t('plant.wizard.unreachable', 'no answer')) };
            } catch (e) { d.probe.modbus = { ok: false, msg: e.message }; }
        })());
        await Promise.all(jobs);
        if (this._plantWiz.step === 1) this._plantWizRender();
        return Object.values(d.probe).every(r => r.ok);
    },

    // ── 2. what it holds ────────────────────────────────────────────────────
    // Groups come from what the datalogger reported, each a tick; anything else
    // is added by hand. Templates follow from role × transport.
    _pwSeedGroups() {
        const d = this._plantWiz.data, ways = this._pwWays(), disc = d.discovered;
        if (d.groups.length) return;
        if (disc && (disc.inverters || []).length) {
            const ids = disc.inverters.map(x => parseInt(x.solar_api_id, 10)).filter(n => !isNaN(n)).sort((a, b) => a - b);
            d.groups.push({ id: 'inverters', role: 'inverter', found: true, on: true, units: ids.join(', '),
                            detail: disc.inverters.map(x => `${x.solar_api_id}${x.dt ? ' · DT ' + x.dt : ''}`).join(' · ') });
            d.groups.push({ id: 'site', role: 'site', found: true, on: ways.http, units: '0',
                            detail: this.t('plant.wizard.siteDetail', 'generation · load · grid · autonomy (Solar API)') });
            (disc.meters || []).forEach(m => {
                d.groups.push({ id: 'grid', role: 'meter', found: true, on: false,
                                units: ways.modbus ? String(m.modbus_unit_hint || 240) : String(m.solar_api_id),
                                meterSolarId: String(m.solar_api_id),
                                detail: `${m.model || 'Smart Meter'}${ways.modbus ? ` · Modbus unit ${m.modbus_unit_hint || 240}` : ` · Solar API id ${m.solar_api_id}`}` });
            });
        } else {
            d.groups.push({ id: 'inverters', role: 'inverter', found: false, on: true, units: '' });
        }
    },

    _pwTemplatesFor(role, transport, templates) {
        const pool = templates || (this._plantWiz || {}).templates || this._srcTemplates || [];
        const roleOf = x => {
            const s = `${x.id} ${x.name || ''}`.toLowerCase();
            if (/site|installation/.test(s)) return 'site';
            if (/meter|sdm|em24|iem3|b2[13]|umg/.test(s)) return 'meter';
            if (/inverter|sunspec/.test(s)) return 'inverter';
            if (/battery|bms/.test(s)) return 'battery';
            if (/sensor|ble|zigbee|mqtt/.test(s)) return 'sensor';
            return '';
        };
        return pool.filter(x =>
            (!transport || x.transport === transport) && (roleOf(x) === role || roleOf(x) === ''));
    },

    _plantWizStep2() {
        const t = (k, d) => this.t(k, d), w = this._plantWiz, d = w.data, ways = this._pwWays();
        this._pwSeedGroups();
        const found = d.groups.filter(g => g.found), manual = d.groups.filter(g => !g.found);
        const foundHtml = found.length ? `
            <div style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px;">${t('plant.wizard.found', 'Found on the datalogger')}</div>
            ${found.map(g => { const i = d.groups.indexOf(g), pre = this.ROLE_PRESETS[g.role];
                return `<label style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light,#2a3038);">
                    <input type="checkbox" data-g="${i}" data-f="on" ${g.on ? 'checked' : ''} style="margin-top:3px;">
                    <span><i aria-hidden="true" class="bi ${pre.icon}"></i> <b>${t('plant.role.' + g.role, pre.label)}</b>${g.role === 'inverter' ? ` (${(this._parseUnitList(g.units) || []).length})` : ''}
                        <span style="color:var(--text-secondary);font-size:12px;"> · ${this._esc(g.detail || '')}</span>
                        ${g.role === 'meter' && !g.on ? `<div class="field-hint">${t('plant.wizard.meterHint', 'Unticked on purpose: grid data is better read straight from the meter (RTU) than through the datalogger.')}</div>` : ''}</span>
                </label>`; }).join('')}`
            : `<div class="field-hint" style="margin-bottom:10px;">${d.discovered ? t('plant.wizard.foundNone', 'The datalogger reported no inverters.') : t('plant.wizard.notAsked', ways.http ? 'The datalogger did not answer, so nothing could be found — add what it holds by hand.' : 'Modbus cannot list what is behind the datalogger — add what it holds by hand.')}</div>`;
        const tplOpts = (g) => {
            const transport = ways.http && !ways.modbus ? 'http' : ways.modbus && !ways.http ? 'modbus' : '';
            const list = this._pwTemplatesFor(g.role, transport);
            return ['<option value="">—</option>'].concat(list.map(x =>
                `<option value="${this._esc(x.id)}" ${g.template === x.id ? 'selected' : ''}>${this._esc(x.name || x.id)} · ${x.transport}</option>`)).join('');
        };
        const manualHtml = manual.map(g => { const i = d.groups.indexOf(g), pre = this.ROLE_PRESETS[g.role] || this.ROLE_PRESETS.inverter;
            return `
            <div class="settings-card" style="margin:10px 0;">
              <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi ${pre.icon}"></i> ${this._esc(g.id || pre.group)}</h3>
                <button class="btn btn-ghost btn-sm" ${d.groups.length < 2 ? 'disabled' : ''} onclick="app.plantWizRemoveGroup(${i})"
                        title="${t('common.delete', 'Remove')}" aria-label="${t('common.delete', 'Remove')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
              </div>
              <div class="settings-card-body">
                <div class="form-row">
                  <div class="form-group"><label class="form-label" for="pwRole${i}">${t('endpoints.groupRole', 'Holds')}</label>
                    <select id="pwRole${i}" class="input" data-g="${i}" data-f="role" onchange="app.plantWizRoleChanged(${i}, this.value)">
                      ${Object.entries(this.ROLE_PRESETS).map(([k, v]) => `<option value="${k}" ${g.role === k ? 'selected' : ''}>${t('plant.role.' + k, v.label)}</option>`).join('')}
                    </select></div>
                  <div class="form-group"><label class="form-label" for="pwGid${i}">${t('endpoints.groupId', 'Group ID')}</label>
                    <input id="pwGid${i}" class="input" data-g="${i}" data-f="id" value="${this._esc(g.id)}"></div>
                  <div class="form-group flex-2"><label class="form-label" for="pwUnits${i}">${t('endpoints.unitsLabel', 'Unit IDs')}</label>
                    <input id="pwUnits${i}" class="input" data-g="${i}" data-f="units" value="${this._esc(g.units)}" placeholder="1, 2, 3, 4">
                    <div class="field-hint">${t('endpoints.unitsHint', 'Comma-separated, ranges allowed (1-4).')}</div></div>
                  <div class="form-group flex-2"><label class="form-label" for="pwTpl${i}">${t('devices.wizard.template', 'Template')}</label>
                    <select id="pwTpl${i}" class="input" data-g="${i}" data-f="template">${tplOpts(g)}</select>
                    <div class="field-hint">${t('plant.wizard.tplHint', 'Only templates for this kind of unit and the way it is reached.')}</div></div>
                </div>
                ${ways.http ? `<div class="form-group"><label class="form-label" for="pwUrl${i}">${t('plant.wizard.httpUrl', 'Solar API / JSON URL')}</label>
                    <input id="pwUrl${i}" class="input" data-g="${i}" data-f="url" value="${this._esc(g.url || '')}" placeholder="http://${this._esc(d.host || 'host')}/solar_api/v1/...DeviceId=\${unit_id}">
                    <div class="field-hint">${t('endpoints.srcUrlHint', 'Use ${unit_id} — an HTTP master addresses its units by URL, not by a unit id inside a frame.')}</div></div>` : ''}
                <div class="field-hint">${t('plant.role.' + g.role + '.hint', pre.hint)}</div>
              </div>
            </div>`; }).join('');
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.holdsIntro',
            'An installation is not one kind of thing: its inverters, the site balance, the meter at the grid. Each is a group, totalled on its own.')}</p>
        ${foundHtml}
        ${manualHtml}
        <button class="btn btn-secondary btn-sm" style="margin-top:10px;" onclick="app.plantWizAddGroup()">
            <i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('plant.wizard.addManual', 'Add a group by hand')}</button>`;
    },

    // ── 3. how often ────────────────────────────────────────────────────────
    _plantWizStep3() {
        const t = (k, d) => this.t(k, d), d = this._plantWiz.data, ways = this._pwWays();
        const active = d.groups.filter(g => g.on !== false);
        const nInv = active.filter(g => g.role === 'inverter').reduce((a, g) => a + (this._parseUnitList(g.units) || []).length, 0);
        const ms = (d.probe.modbus || {}).ms;
        const floor = ms && nInv ? (ms / 1000) * 3 * nInv : null;
        const num = (i, f, val, min, label) => `<label style="display:flex;align-items:center;gap:8px;font-size:13px;">${label}
            <input class="input" type="number" min="${min}" step="1" style="width:84px;" data-g="${i}" data-f="${f}" value="${val}" aria-label="${label.replace(/<[^>]+>/g, '')}"> s</label>`;
        const way = (name, ...lines) => `<div style="display:grid;grid-template-columns:90px 1fr;gap:6px 12px;align-items:center;">
            <b>${name}</b>${lines.map((l, k) => `${k ? '<span></span>' : ''}${l}`).join('')}</div>`;
        const rows = active.map(g => { const i = d.groups.indexOf(g), pre = this.ROLE_PRESETS[g.role] || this.ROLE_PRESETS.inverter;
            const http = ways.http && (g.role !== 'meter' || !ways.modbus) && (g.found || g.url || g.role === 'inverter' || g.role === 'site');
            const modbus = ways.modbus && g.role !== 'site';
            return `
            <div class="settings-card" style="margin-bottom:12px;">
              <div class="settings-card-header"><h3><i aria-hidden="true" class="bi ${pre.icon}"></i> ${t('plant.role.' + g.role, pre.label)}</h3></div>
              <div class="settings-card-body" style="display:flex;flex-direction:column;gap:10px;">
                ${http ? way('Solar API',
                    num(i, 'httpEvery', g.httpEvery || 2, 1, t('plant.wizard.fastEvery', 'power, voltages, currents every')),
                    num(i, 'energyEvery', g.energyEvery || 30, 5, t('plant.wizard.energyEvery', 'energy counters every'))) : ''}
                ${modbus ? way('Modbus',
                    num(i, 'modbusEvery', g.modbusEvery || 20, 5, t('plant.wizard.fullEvery', 'the complete reading (SunSpec) every')),
                    num(i, 'slowEvery', g.slowEvery || 120, 30, t('plant.wizard.slowEvery', 'counters and static data every'))) : ''}
                ${modbus && g.role === 'inverter' && floor && ms >= 200 ? `<div class="field-hint"><i aria-hidden="true" class="bi bi-info-circle"></i>
                    ${t('plant.wizard.floorHint', 'The datalogger answered in')} ~${(ms / 1000).toFixed(1)} s ${t('plant.wizard.floorHint2', 'per read;')} ${nInv} × 3 ${t('plant.wizard.floorHint3', 'reads → below')} ~${Math.ceil(floor * 2)} s ${t('plant.wizard.floorHint4', 'it will not keep up.')}</div>` : ''}
              </div>
            </div>`; }).join('');
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.readIntro',
            'How often each group is read. Solar API is cheap; Modbus on this datalogger is not — the measured floor is shown where it matters.')}</p>
        ${rows}
        ${ways.http && ways.modbus ? `<div class="field-hint" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            ${t('plant.wizard.precedence', 'Solar API (fast, partial) goes first and Modbus fills in the rest. If the Solar API goes quiet for')}
            <input class="input" type="number" min="5" step="1" style="width:84px;" id="pwStale" value="${d.staleAfter || 30}" aria-label="${t('plant.wizard.staleLabel', 'stale after seconds')}"> s
            ${t('plant.wizard.precedence2', 'Modbus takes its fields back on its own.')}</div>` : ''}`;
    },

    // ── 4. review ───────────────────────────────────────────────────────────
    _plantWizStep4() {
        const t = (k, d) => this.t(k, d), d = this._plantWiz.data;
        const body = this._plantWizBody();
        const rows = body.groups.map((g, i) => {
            const pre = this.ROLE_PRESETS[g.role] || this.ROLE_PRESETS.inverter;
            const units = (g.units || []).map(u => typeof u === 'object' ? u.unit_id : u);
            const srcs = (g.sources || []).map(s => { const iv = Math.min(...Object.values(s.poll_groups || {}).map(x => x.interval)); return `${s.id} ${t('endpoints.every', 'every')} ${iv} s`; }).join(' · ');
            const topic = ((g.mqtt || {}).topic_prefix || (body.mqtt || {}).topic_prefix || `mbg/endpoints/${d.id}${i ? '/' + g.id : ''}`).replace(/\$\{unit_id\}/g, 'N');
            const totals = units.length > 1 ? ((body.mqtt || {}).aggregate_prefix || `mbg/endpoints/${d.id}${i ? '/' + g.id : ''}`).replace(/\$\{group_id\}/g, g.id) : null;
            return `<tr>
                <td style="padding:4px 12px 4px 0;"><b>${t('plant.role.' + g.role, pre.label)}</b> <span class="dev-chip">${this._esc(g.id)}</span></td>
                <td style="padding:4px 12px 4px 0;">${units.length} ${units.length === 1 ? t('endpoints.unit', 'unit') : t('endpoints.units', 'units')} (${units.join(', ')})</td>
                <td style="padding:4px 12px 4px 0;">${this._esc(srcs)}</td>
                <td style="padding:4px 0;"><code style="font-size:11px;">${this._esc(topic)}/…</code>${totals ? `<br><span style="color:var(--text-secondary);font-size:11px;">${t('endpoints.totalsTopic', 'Totals')} <code style="font-size:11px;">${this._esc(totals)}/…</code></span>` : ''}</td>
            </tr>`;
        }).join('');
        const devs = body.groups.flatMap(g => (g.units || []).map(u => typeof u === 'object' ? u.id : `${d.id}-u${u}`));
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.reviewIntro',
            'What will be created. Every unit becomes its own device with its own history, read exactly as shown.')}</p>
        <div style="overflow-x:auto;"><table style="width:100%;font-size:13px;">
          <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
            <td>${t('endpoints.group', 'group')}</td><td>${t('endpoints.unitsTitle', 'units')}</td>
            <td>${t('endpoints.readVia', 'read via')}</td><td>${t('endpoints.publishesTo', 'publishes to')}</td></tr>
          ${rows}
        </table></div>
        <p class="field-hint" style="margin-top:14px;"><i aria-hidden="true" class="bi bi-cpu"></i>
          ${t('plant.wizard.willCreate', 'Devices to be created')}: <code>${devs.map(x => this._esc(x)).join('</code>, <code>')}</code>
          · ${t('plant.wizard.startsNow', 'Reading starts as soon as it is created.')}</p>`;
    },

    // ── state ───────────────────────────────────────────────────────────────
    _plantWizCollect() {
        const w = this._plantWiz, d = w.data;
        const v = id => (document.getElementById(id) || {}).value;
        if (w.step === 1) {
            if (document.getElementById('pwName')) {
                d.name = (v('pwName') || '').trim();
                d.id = (v('pwId') || '').trim().toLowerCase();
                d.ways = document.querySelector('input[name="pwWays"]:checked')?.value || d.ways;
                d.host = (v('pwHost') || '').trim();
                if (document.getElementById('pwPort')) d.port = parseInt(v('pwPort'), 10) || 502;
                d.root = (v('pwRoot') || '').trim().replace(/^\/+|\/+$/g, '');
                if (!d.id && d.name) d.id = this._pwSlug(d.name);
            }
        } else {
            document.querySelectorAll('#plantWizBody [data-g]').forEach(el => {
                const g = d.groups[parseInt(el.dataset.g, 10)];
                if (!g) return;
                g[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value;
            });
            const st = document.getElementById('pwStale');
            if (st) d.staleAfter = parseInt(st.value, 10) || 30;
        }
    },

    plantWizCollectAndRender() { this._plantWizCollect(); this._plantWizRender(); },

    plantWizAddGroup() {
        this._plantWizCollect();
        const used = new Set(this._plantWiz.data.groups.map(g => g.role));
        const role = Object.keys(this.ROLE_PRESETS).find(r => !used.has(r)) || 'meter';
        const pre = this.ROLE_PRESETS[role];
        this._plantWiz.data.groups.push({ id: pre.group, role, template: '', units: '', on: true, found: false });
        this._plantWizRender();
    },

    plantWizRemoveGroup(i) {
        this._plantWizCollect();
        if (this._plantWiz.data.groups.length < 2) return;
        this._plantWiz.data.groups.splice(i, 1);
        this._plantWizRender();
    },

    plantWizRoleChanged(i, role) {
        this._plantWizCollect();
        const g = this._plantWiz.data.groups[i];
        const pre = this.ROLE_PRESETS[role];
        const wasPreset = Object.values(this.ROLE_PRESETS).some(v => v.group === g.id);
        g.role = role;
        if (wasPreset && pre) g.id = pre.group;
        g.template = '';
        this._plantWizRender();
    },

    // the payload the API expects, built from the wizard's own shape
    _plantWizBody() {
        const d = this._plantWiz.data, ways = this._pwWays();
        const base = `http://${d.host}`;
        const root = d.root ? d.root.replace(/\/+$/, '') : '';
        const stale = ways.http && ways.modbus ? (d.staleAfter || 30) : 0;
        const groups = d.groups.filter(g => g.on !== false).map(g => {
            const gid = (g.id || '').trim().toLowerCase();
            const ids = this._parseUnitList(g.units) || [];
            const units = g.role === 'site' ? [{ unit_id: ids[0] ?? 0, id: `${d.id}-site`, name: `${d.name || d.id} · ${this.t('plant.role.site', 'Site totals')}` }]
                : g.role === 'meter' && g.found ? ids.map(u => ({ unit_id: u, id: `${d.id}-meter-${u}`, name: this.t('plant.role.meter', 'Grid meter') }))
                : ids;
            const out = { id: gid, role: g.role || '', enabled: true, units };
            if (root) out.mqtt = { topic_prefix: units.length === 1 && g.role !== 'inverter' ? `${root}/${gid}` : `${root}/${gid}/\${unit_id}` };
            const sources = [];
            // a bundled template is used only if this gateway actually has it
            const has = id => !!id && this._plantWiz.templates.some(x => x.id === id);
            const chosenT = (g.template && (this._plantWiz.templates.find(x => x.id === g.template) || {}).transport) || '';
            const httpTpl = chosenT === 'http' ? g.template : (has(this._TPL_FOR[`${g.role}:http`]) ? this._TPL_FOR[`${g.role}:http`] : '');
            const mbTpl = chosenT === 'modbus' ? g.template : (has(this._TPL_FOR[`${g.role}:modbus`]) ? this._TPL_FOR[`${g.role}:modbus`] : '');
            const url = g.found ? (this._SOLAR_API[g.role] ? base + this._SOLAR_API[g.role] : '') : (g.url || '').trim();
            const wantHttp = ways.http && url && httpTpl && !(g.role === 'meter' && ways.modbus);
            const wantModbus = ways.modbus && g.role !== 'site' && mbTpl;
            if (wantHttp) sources.push({ id: 'solar_api', protocol: 'http', url, template: httpTpl, timeout: 5,
                poll_groups: { realtime: { interval: parseFloat(g.httpEvery) || 2 }, normal: { interval: parseFloat(g.energyEvery) || 30 } },
                stale_after_s: wantModbus ? stale : 0 });
            if (wantModbus) sources.push({ id: 'sunspec', protocol: 'tcp', host: this._pwHostPort().host, port: d.port, timeout: 3, template: mbTpl,
                poll_groups: { normal: { interval: parseFloat(g.modbusEvery) || 20 }, slow: { interval: parseFloat(g.slowEvery) || 120 } },
                stale_after_s: 0 });
            out.sources = sources;
            return out;
        }).filter(g => g.sources.length);       // a group nothing can read is not created
        const body = { id: d.id, name: d.name || d.id, enabled: true,
                       connection: ways.modbus ? { protocol: 'tcp', host: this._pwHostPort().host, port: d.port } : {},
                       units: groups[0] ? groups[0].units : [], groups };
        if (root) {
            body.mqtt = { topic_prefix: `${root}/units/\${unit_id}`, aggregate_prefix: `${root}/\${group_id}/summary` };
            body.influxdb = { bucket: root, device_tag: '${device_id}' };
        }
        return body;
    },

    plantWizardBack() {
        this._plantWizCollect();
        if (this._plantWiz.step > 1) { this._plantWiz.step--; this._plantWizRender(); }
    },

    async plantWizardNext() {
        this._plantWizCollect();
        const w = this._plantWiz, d = w.data, ways = this._pwWays();
        const fb = document.getElementById('plantWizFeedback');
        fb.textContent = '';
        if (w.step === 1) {
            if (!/^[a-z0-9][a-z0-9_-]{1,63}$/.test(d.id)) {
                fb.textContent = this.t('plant.wizard.badId', 'ID: use a-z 0-9 - _ (2-64 chars, starts alphanumeric).'); return;
            }
            if (!d.host) { fb.textContent = this.t('plant.wizard.needHost', 'Enter the datalogger address first.'); return; }
            if (d.root && !/^[a-z0-9][a-z0-9_\/-]{0,63}$/.test(d.root)) {
                fb.textContent = this.t('plant.wizard.badRoot', 'Publishes under: a-z 0-9 - _ /'); return;
            }
            // ask the datalogger what it holds — the Test may have done it already
            if (ways.http && !d.discovered) {
                const btn = document.getElementById('plantWizNext'); btn.disabled = true;
                fb.textContent = this.t('plant.wizard.asking', 'Asking the datalogger what it holds…');
                try { await this.plantWizProbe(true); } finally { btn.disabled = false; }
                fb.textContent = '';
            }
            d.groups = [];   // seeded from what was found, on entering step 2
        }
        if (w.step === 2) {
            const active = d.groups.filter(g => g.on !== false);
            if (!active.length) { fb.textContent = this.t('plant.wizard.needGroup', 'Tick or add at least one group.'); return; }
            const seen = new Set();
            for (const g of active) {
                const gid = (g.id || '').trim().toLowerCase();
                if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(gid)) { fb.textContent = this.t('plant.wizard.badGroupId', 'Group ID: use a-z 0-9 - _ (1-32 chars).'); return; }
                const u = this._parseUnitList(g.units);
                if (!u || !u.length) { fb.textContent = this.t('plant.wizard.needUnits', `Group "${gid}": enter its unit IDs.`); return; }
                if (!g.found && !g.template) { fb.textContent = this.t('plant.wizard.needTemplate', `Group "${gid}": choose a template.`); return; }
                if (!g.found && ways.http && !ways.modbus && !(g.url || '').trim()) { fb.textContent = this.t('plant.wizard.needUrl', `Group "${gid}": the Solar API / JSON source needs a URL.`); return; }
                for (const x of u) {
                    if (seen.has(x)) { fb.textContent = this.t('plant.wizard.dupUnit', `Unit ${x} is claimed by two groups — one address, one device.`); return; }
                    seen.add(x);
                }
            }
        }
        if (w.step < 4) { w.step++; this._plantWizRender(); return; }

        const btn = document.getElementById('plantWizNext');
        btn.disabled = true;
        try {
            const rsp = await fetch('/api/endpoints', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this._plantWizBody()),
            });
            if (!rsp.ok) {
                const j = await rsp.json().catch(() => ({}));
                fb.textContent = (j.detail?.errors || [j.detail || rsp.statusText]).join(' · ');
                return;
            }
            this.closeModal('plantWizardModal');
            this.showToast(this.t('plant.wizard.created', 'Installation created'), 'success');
            await this.renderDevicesList?.();
            this.openEndpointDetail?.(d.id);
        } finally {
            btn.disabled = false;
        }
    },
});
