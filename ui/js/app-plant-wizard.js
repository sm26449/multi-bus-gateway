// Plant wizard — adding an INSTALLATION, not a connection.
//
// Adding a device asked three questions and a plant asked one, which had it
// backwards: a plant is the bigger object. It has an access point, it holds
// more than one kind of thing (inverters, and usually the meter at its grid
// connection), and each of those may be reachable more than one way. This walks
// that in four steps and leaves nothing to be typed into YAML afterwards.
Object.assign(JanitzaMonitor.prototype, {

    ROLE_PRESETS: {
        inverter: { icon: 'bi-sun', label: 'Inverters', group: 'inverters',
                    hint: 'The units that generate. Their powers add up.' },
        meter:    { icon: 'bi-speedometer2', label: 'Meters', group: 'grid',
                    hint: 'A meter at a measuring point — grid, consumption, a sub-circuit. Totalled on its own, never added to generation.' },
        battery:  { icon: 'bi-battery-half', label: 'Batteries', group: 'battery',
                    hint: 'Storage units.' },
        sensor:   { icon: 'bi-thermometer-half', label: 'Sensors', group: 'sensors',
                    hint: 'Temperature, humidity, irradiance…' },
    },

    async openPlantWizard() {
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        this._plantWiz = {
            step: 1, templates,
            data: {
                id: '', name: '', host: '', port: 502, protocol: 'tcp',
                enabled: true,
                // A plant starts with the group nearly every plant has. A user
                // who wants something else changes it in one click; a user who
                // wants the common thing types nothing.
                groups: [{ id: 'inverters', role: 'inverter', template: '',
                           units: '', enabled: true, modbus: true, http: false }],
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
        const first = body.querySelector('input:not([type=checkbox]), select');
        if (first) setTimeout(() => first.focus(), 30);
    },

    // ── 1. the installation and its access point ────────────────────────────
    _plantWizStep1() {
        const t = (k, d) => this.t(k, d), d = this._plantWiz.data;
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.intro',
            'An installation is one site behind one master device. Give it a name and the address of the master device that fronts it — a datalogger, a gateway, a bridge. What it holds comes next.')}</p>
        <div class="form-row">
            <div class="form-group"><label class="form-label" for="pwId">${t('plant.wizard.id', 'Plant ID')}</label>
                <input id="pwId" class="input" value="${this._esc(d.id)}" placeholder="fronius">
                <div class="field-hint">${t('plant.wizard.idHint', 'a-z 0-9 - _ . Used in topics and device names, and fixed after creation.')}</div></div>
            <div class="form-group flex-2"><label class="form-label" for="pwName">${t('devices.wizard.name', 'Name')}</label>
                <input id="pwName" class="input" value="${this._esc(d.name)}" placeholder="Fronius PV"></div>
        </div>
        <div class="form-row">
            <div class="form-group"><label class="form-label" for="pwProto">${t('devices.wizard.protocol', 'Protocol')}</label>
                <select id="pwProto" class="input">
                    <option value="tcp" ${d.protocol === 'tcp' ? 'selected' : ''}>Modbus TCP</option>
                    <option value="rtu-tcp" ${d.protocol === 'rtu-tcp' ? 'selected' : ''}>Modbus RTU over TCP</option>
                </select>
                <div class="field-hint">${t('plant.wizard.protoHint',
                    'How the master is addressed. An HTTP interface such as the Fronius Solar API is added per group in step 3 — it can sit beside Modbus or replace it entirely.')}</div></div>
            <div class="form-group flex-2"><label class="form-label" for="pwHost">${t('endpoints.host', 'Host')}</label>
                <input id="pwHost" class="input" value="${this._esc(d.host)}" placeholder="192.168.1.50"></div>
            <div class="form-group"><label class="form-label" for="pwPort">${t('endpoints.port', 'Port')}</label>
                <input id="pwPort" class="input" type="number" value="${d.port}"></div>
            <div class="form-group" style="align-self:flex-end;">
                <button class="btn btn-secondary btn-sm" onclick="app.plantWizProbe()" id="pwProbeBtn">
                    <i aria-hidden="true" class="bi bi-activity"></i> ${t('plant.wizard.probe', 'Test')}</button></div>
        </div>
        <div id="pwProbeOut" style="margin-top:6px;"></div>`;
    },

    // ── 2. what the installation holds ──────────────────────────────────────
    _plantWizStep2() {
        const t = (k, d) => this.t(k, d), w = this._plantWiz, d = w.data;
        const tplOpts = (sel) => ['<option value="">—</option>'].concat(w.templates.map(x =>
            `<option value="${this._esc(x.id)}" ${sel === x.id ? 'selected' : ''}>${this._esc(x.name || x.id)}</option>`)).join('');
        const cards = d.groups.map((g, i) => {
            const pre = this.ROLE_PRESETS[g.role] || this.ROLE_PRESETS.inverter;
            return `
            <div class="settings-card" style="margin-bottom:12px;">
              <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi ${pre.icon}"></i> ${this._esc(g.id || pre.group)}</h3>
                <button class="btn btn-ghost btn-sm" ${d.groups.length < 2 ? 'disabled' : ''}
                        onclick="app.plantWizRemoveGroup(${i})"
                        title="${d.groups.length < 2 ? t('plant.wizard.needOne', 'A plant needs at least one group') : t('common.delete', 'Remove')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
              </div>
              <div class="settings-card-body">
                <div class="form-row">
                  <div class="form-group"><label class="form-label">${t('endpoints.groupRole', 'Holds')}</label>
                    <select class="input" data-g="${i}" data-f="role" onchange="app.plantWizRoleChanged(${i}, this.value)">
                      ${Object.entries(this.ROLE_PRESETS).map(([k, v]) =>
                        `<option value="${k}" ${g.role === k ? 'selected' : ''}>${t('plant.role.' + k, v.label)}</option>`).join('')}
                    </select></div>
                  <div class="form-group"><label class="form-label">${t('endpoints.groupId', 'Group ID')}</label>
                    <input class="input" data-g="${i}" data-f="id" value="${this._esc(g.id)}"></div>
                  <div class="form-group flex-2"><label class="form-label">${t('devices.wizard.template', 'Template')}</label>
                    <select class="input" data-g="${i}" data-f="template">${tplOpts(g.template)}</select></div>
                  <div class="form-group flex-2"><label class="form-label">${t('endpoints.unitsLabel', 'Unit IDs')}</label>
                    <input class="input" data-g="${i}" data-f="units" value="${this._esc(g.units)}" placeholder="1, 2, 3, 4">
                    <div class="field-hint">${t('endpoints.unitsHint', 'Comma-separated, ranges allowed (1-4).')}</div></div>
                </div>
                <div class="field-hint">${t('plant.role.' + g.role + '.hint', pre.hint)}</div>
              </div>
            </div>`;
        }).join('');
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.holdsIntro',
            'An installation is not one kind of thing. Add a group for each: the inverters, the meter at the grid connection, a battery. Groups are totalled separately, because adding a meter\'s power to the inverters\' would describe nothing.')}</p>
        ${cards}
        <button class="btn btn-secondary btn-sm" onclick="app.plantWizAddGroup()">
            <i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('endpoints.groupAdd', 'Add group')}</button>`;
    },

    // ── 3. how each group is read ───────────────────────────────────────────
    _plantWizStep3() {
        const t = (k, d) => this.t(k, d), d = this._plantWiz.data;
        const rows = d.groups.map((g, i) => `
            <div class="settings-card" style="margin-bottom:12px;">
              <div class="settings-card-header"><h3>${this._esc(g.id)}</h3></div>
              <div class="settings-card-body">
                <div class="form-row">
                  <div class="form-group flex-2"><label class="form-label">${t('endpoints.srcGroups', 'Intervals')}</label>
                    <input class="input" data-g="${i}" data-f="intervals"
                           value="${this._esc(g.intervals || 'normal=20, slow=120')}" placeholder="normal=20, slow=120">
                    <div class="field-hint">${t('endpoints.srcGroupsHint', 'group=seconds, comma separated. This source polls ONLY the groups named here.')}</div></div>
                  <div class="form-group"><label class="form-label">${t('endpoints.srcTimeout', 'Timeout (s)')}</label>
                    <input class="input" type="number" min="1" data-g="${i}" data-f="timeout" value="${g.timeout || 3}"></div>
                </div>
                <div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:6px;">
                  <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" data-g="${i}" data-f="modbus" ${g.modbus !== false ? 'checked' : ''}
                           onchange="app.plantWizCollectAndRender()">
                    ${t('plant.wizard.useModbus', 'Read over Modbus')}</label>
                  <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" data-g="${i}" data-f="http" ${g.http ? 'checked' : ''}
                           onchange="app.plantWizCollectAndRender()">
                    ${t('plant.wizard.useHttp', 'Read over HTTP / Solar API')}</label>
                </div>
                ${g.modbus === false && !g.http ? `<div class="field-hint" style="color:var(--danger,#ef4444);">
                    ${t('plant.wizard.needAWay', 'A group needs at least one way to be read.')}</div>` : ''}
                ${g.http ? `<div style="margin-top:10px;">
                  <span style="color:var(--text-secondary);font-size:12px;">${t('plant.wizard.preset', 'Preset')}:</span>
                  <button class="btn btn-ghost btn-sm" onclick="app.plantWizPreset(${i},'fronius_inverter')">Fronius · ${t('plant.role.inverter', 'inverters')}</button>
                  <button class="btn btn-ghost btn-sm" onclick="app.plantWizPreset(${i},'fronius_meter')">Fronius · ${t('plant.role.meter', 'meter')}</button>
                </div>
                <div class="form-row" style="margin-top:8px;">
                  <div class="form-group flex-2"><label class="form-label">URL</label>
                    <input class="input" data-g="${i}" data-f="url" value="${this._esc(g.url || '')}"
                           placeholder="http://host/solar_api/v1/...DeviceId=\${unit_id}">
                    <div class="field-hint">${t('endpoints.srcUrlHint', 'Use ${unit_id} — an HTTP master addresses its units by URL, not by a unit id inside a frame.')}</div></div>
                  <div class="form-group flex-2"><label class="form-label">${t('devices.wizard.template', 'Template')}</label>
                    <select class="input" data-g="${i}" data-f="httpTemplate">
                      ${['<option value="">—</option>'].concat(this._plantWiz.templates.map(x =>
                        `<option value="${this._esc(x.id)}" ${g.httpTemplate === x.id ? 'selected' : ''}>${this._esc(x.name || x.id)}</option>`)).join('')}
                    </select></div>
                  <div class="form-group"><label class="form-label">${t('plant.wizard.httpEvery', 'Every (s)')}</label>
                    <input class="input" type="number" min="1" data-g="${i}" data-f="httpEvery" value="${g.httpEvery || 5}"></div>
                </div>
                <div class="field-hint"><i aria-hidden="true" class="bi bi-info-circle"></i>
                  ${t('plant.wizard.httpNote', 'It is placed FIRST, so it owns every field it offers and the Modbus source fills in the rest. If it goes quiet for 30 s, Modbus takes those fields back on its own.')}</div>` : ''}
              </div>
            </div>`).join('');
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.readIntro',
            'How often each group is read, and whether there is a second, faster way to reach it. A datalogger often answers HTTP far quicker than Modbus while carrying fewer fields — you can have both.')}</p>
        ${rows}`;
    },

    // ── 4. review ───────────────────────────────────────────────────────────
    _plantWizStep4() {
        const t = (k, d) => this.t(k, d), d = this._plantWiz.data;
        const body = this._plantWizBody();
        const rows = body.groups.map((g, i) => {
            const units = (g.units || []).length;
            const srcs = (g.sources || []).map(s => `${s.id} (${s.protocol})`).join(' → ')
                || `default (${(g.connection || {}).protocol || 'tcp'})`;
            return `<tr>
                <td style="padding:4px 12px 4px 0;"><b>${this._esc(g.id)}</b></td>
                <td style="padding:4px 12px 4px 0;">${this._esc(g.role || '—')}</td>
                <td style="padding:4px 12px 4px 0;">${units} ${t('endpoints.unitsTitle', 'units')}</td>
                <td style="padding:4px 12px 4px 0;">${this._esc(g.template || '—')}</td>
                <td style="padding:4px 12px 4px 0;">${this._esc(srcs)}</td>
                <td style="padding:4px 0;"><code style="font-size:11px;">mbg/endpoints/${this._esc(d.id)}${i ? '/' + this._esc(g.id) : ''}/…</code></td>
            </tr>`;
        }).join('');
        const devs = body.groups.flatMap(g => (g.units || []).map(u =>
            typeof u === 'object' ? u.id : `${d.id}-u${u}`));
        return `
        <p class="field-hint" style="margin:0 0 14px;">${t('plant.wizard.reviewIntro',
            'What will be created. Every unit becomes its own device with its own history; the first group owns the plant\'s headline topic.')}</p>
        <div style="overflow-x:auto;"><table style="width:100%;font-size:13px;">
          <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
            <td>${t('endpoints.groups', 'group')}</td><td>${t('endpoints.groupRole', 'holds')}</td>
            <td>${t('endpoints.unitsTitle', 'units')}</td><td>${t('devices.wizard.template', 'template')}</td>
            <td>${t('endpoints.sources', 'sources')}</td><td>${t('endpoints.groupTopic', 'publishes on')}</td></tr>
          ${rows}
        </table></div>
        <p class="field-hint" style="margin-top:14px;"><i aria-hidden="true" class="bi bi-cpu"></i>
          ${t('plant.wizard.willCreate', 'Devices to be created')}: <code>${devs.map(x => this._esc(x)).join('</code>, <code>')}</code></p>`;
    },

    // ── state ───────────────────────────────────────────────────────────────
    _plantWizCollect() {
        const w = this._plantWiz, d = w.data;
        const v = id => (document.getElementById(id) || {}).value;
        if (w.step === 1) {
            d.id = (v('pwId') || '').trim().toLowerCase();
            d.name = (v('pwName') || '').trim();
            d.protocol = v('pwProto') || 'tcp';
            d.host = (v('pwHost') || '').trim();
            d.port = parseInt(v('pwPort'), 10) || 502;
            if (!d.name) d.name = d.id;
        } else {
            document.querySelectorAll('#plantWizBody [data-g]').forEach(el => {
                const g = d.groups[parseInt(el.dataset.g, 10)];
                if (!g) return;
                g[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value;
            });
        }
    },

    plantWizCollectAndRender() { this._plantWizCollect(); this._plantWizRender(); },

    plantWizAddGroup() {
        this._plantWizCollect();
        const used = new Set(this._plantWiz.data.groups.map(g => g.role));
        const role = Object.keys(this.ROLE_PRESETS).find(r => !used.has(r)) || 'meter';
        const pre = this.ROLE_PRESETS[role];
        this._plantWiz.data.groups.push({ id: pre.group, role, template: '',
                                          units: '', enabled: true,
                                          modbus: true, http: false });
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
        // only rename the group when the id was still the previous preset's —
        // never clobber something the operator typed
        const wasPreset = Object.values(this.ROLE_PRESETS).some(v => v.group === g.id);
        g.role = role;
        if (wasPreset && pre) g.id = pre.group;
        this._plantWizRender();
    },

    // Typing a Solar API URL by hand is how a plant ends up silently reading
    // nothing: one wrong query parameter still returns 200 OK with an empty
    // body. The presets fill the exact call, with ${unit_id} in place.
    plantWizPreset(i, kind) {
        this._plantWizCollect();
        const d = this._plantWiz.data, g = d.groups[i];
        const host = d.host || '<host>';
        if (kind === 'fronius_inverter') {
            g.url = `http://${host}/solar_api/v1/GetInverterRealtimeData.cgi`
                  + '?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData';
            if (this._plantWiz.templates.some(x => x.id === 'fronius_solar_api_inverter')) {
                g.httpTemplate = 'fronius_solar_api_inverter';
            }
        } else if (kind === 'fronius_meter') {
            g.url = `http://${host}/solar_api/v1/GetMeterRealtimeData.cgi`
                  + '?Scope=Device&DeviceId=${unit_id}';
            if (this._plantWiz.templates.some(x => x.id === 'fronius_solar_api')) {
                g.httpTemplate = 'fronius_solar_api';
            }
        }
        g.httpEvery = g.httpEvery || 5;
        this._plantWizRender();
    },

    async plantWizProbe() {
        this._plantWizCollect();
        const d = this._plantWiz.data;
        const out = document.getElementById('pwProbeOut');
        if (!d.host) { out.innerHTML = `<span style="color:var(--danger,#ef4444);">${this.t('plant.wizard.needHost', 'Enter a host first.')}</span>`; return; }
        out.innerHTML = this.t('common.loading', 'Loading…');
        try {
            const r = await fetch('/api/devices/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ connection: { protocol: d.protocol, host: d.host,
                                                     port: d.port, unit_id: 1 } }),
            });
            const j = await r.json();
            const ok = j.ok || j.success;
            out.innerHTML = `<span style="color:${ok ? 'var(--success,#22c55e)' : 'var(--danger,#ef4444)'};">`
                + `${ok ? '✓ ' + this.t('plant.wizard.reachable', 'reachable')
                        : '✗ ' + this._esc(j.error || j.message || this.t('plant.wizard.unreachable', 'no answer'))}</span>`;
        } catch (e) {
            out.innerHTML = `<span style="color:var(--danger,#ef4444);">${this._esc(e.message)}</span>`;
        }
    },

    // the payload the API expects, built from the wizard's own shape
    _plantWizBody() {
        const d = this._plantWiz.data;
        const groups = d.groups.map(g => {
            const units = this._parseUnitList(g.units) || [];
            const out = { id: (g.id || '').trim().toLowerCase(), role: g.role || '',
                          enabled: g.enabled !== false, units };
            if (g.template) out.template = g.template;
            const conn = { protocol: d.protocol, host: d.host, port: d.port,
                           timeout: parseInt(g.timeout, 10) || 3 };
            const groupsOf = txt => {
                const o = {};
                for (const part of String(txt || '').split(',')) {
                    const m = part.trim().match(/^([a-z0-9_-]+)\s*=\s*([\d.]+)$/i);
                    if (m) o[m[1]] = { interval: parseFloat(m[2]) };
                }
                return o;
            };
            const modbus = { id: 'modbus', protocol: d.protocol, host: d.host,
                             port: d.port, timeout: parseInt(g.timeout, 10) || 3,
                             stale_after_s: 0,
                             ...(g.template ? { template: g.template } : {}),
                             poll_groups: groupsOf(g.intervals || 'normal=20, slow=120') };
            const useHttp = g.http && (g.url || '').trim();
            const useModbus = g.modbus !== false;
            const http = {
                id: 'solar_api', protocol: 'http', url: (g.url || '').trim(),
                ...(g.httpTemplate ? { template: g.httpTemplate } : {}),
                poll_groups: { realtime: { interval: parseFloat(g.httpEvery) || 5 } },
                // it yields only if it goes quiet; with Modbus beside it that is
                // automatic failover, and alone the window costs nothing
                stale_after_s: useModbus ? 30 : 0, timeout: 5,
            };
            // The faster source goes FIRST: it owns every field it offers and
            // the other fills in the rest.
            const sources = [...(useHttp ? [http] : []),
                             ...(useModbus ? [modbus] : [])];
            out.sources = sources;
            if (!useHttp) out.connection = conn;
            return out;
        });
        return { id: d.id, name: d.name || d.id, enabled: true,
                 connection: { protocol: d.protocol, host: d.host, port: d.port },
                 units: groups[0] ? groups[0].units : [], groups };
    },

    plantWizardBack() {
        this._plantWizCollect();
        if (this._plantWiz.step > 1) { this._plantWiz.step--; this._plantWizRender(); }
    },

    async plantWizardNext() {
        this._plantWizCollect();
        const w = this._plantWiz, d = w.data;
        const fb = document.getElementById('plantWizFeedback');
        fb.textContent = '';
        if (w.step === 1) {
            if (!/^[a-z0-9][a-z0-9_-]{1,63}$/.test(d.id)) {
                fb.textContent = this.t('plant.wizard.badId', 'Plant ID: use a-z 0-9 - _ (2-64 chars, starts alphanumeric).'); return;
            }
            if (!d.host) { fb.textContent = this.t('plant.wizard.needHost', 'Enter a host first.'); return; }
        }
        if (w.step === 2) {
            for (const g of d.groups) {
                if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test((g.id || '').trim().toLowerCase())) {
                    fb.textContent = this.t('plant.wizard.badGroupId', 'Group ID: use a-z 0-9 - _ (1-32 chars).'); return;
                }
                const u = this._parseUnitList(g.units);
                if (!u || !u.length) {
                    fb.textContent = this.t('plant.wizard.needUnits', `Group "${g.id}": enter its unit IDs.`); return;
                }
                if (!g.template) {
                    fb.textContent = this.t('plant.wizard.needTemplate', `Group "${g.id}": choose a template.`); return;
                }
            }
            const seen = new Set();
            for (const g of d.groups) {
                for (const u of (this._parseUnitList(g.units) || [])) {
                    if (seen.has(u)) {
                        fb.textContent = this.t('plant.wizard.dupUnit',
                            `Unit ${u} is claimed by two groups — one address, one device.`); return;
                    }
                    seen.add(u);
                }
            }
        }
        if (w.step === 3) {
            for (const g of d.groups) {
                if (g.modbus === false && !g.http) {
                    fb.textContent = this.t('plant.wizard.needAWay2',
                        `Group "${g.id}": choose at least one way to read it.`); return;
                }
                if (g.http && !(g.url || '').trim()) {
                    fb.textContent = this.t('plant.wizard.needUrl',
                        `Group "${g.id}": the HTTP source needs a URL.`); return;
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
