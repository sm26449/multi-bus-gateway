// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
//
// bridges — augments JanitzaMonitor.prototype
//
// A bridge is the box a serial bus is reached through: our ser2net container
// on a Raspberry Pi, a Waveshare / USR / Elfin converter. It lives on the
// Devices page with its buses (ports) and the devices on each; what a KIND of
// bridge can do comes from the server (bridge_types/*.json), never from here.

Object.assign(JanitzaMonitor.prototype, {

    // the bus monitor (Diagnostics), showing only this bus's traffic
    openBusMonitor(bus) {
        this._diagBus = bus;
        const sel = document.getElementById('diagBusFilter');
        if (sel) sel.value = bus;
        this.navigateTo('diagnostics');
    },

    async _loadBridges() {
        try {
            const [b, t] = await Promise.all([fetch('/api/bridges'), fetch('/api/bridge-types')]);
            this._bridges = (await b.json()).bridges || [];
            this._bridgeTypes = (await t.json()).types || [];
        } catch (e) { this._bridges = this._bridges || []; this._bridgeTypes = this._bridgeTypes || []; }
        return this._bridges;
    },

    _bridgeType(id) { return (this._bridgeTypes || []).find(t => t.id === id) || {}; },

    // ── a serial port the bridge lists (bridge-ports.js): what it is, and
    //    for one it cannot serve, why and what fixes it ────────────────────
    _portStatusText(st) {
        const t = (k, d) => this.t(k, d);
        return ({
            ok: t('bridges.st.ok', 'ready'),
            busy: t('bridges.st.busy', 'in use'),
            'no-access': t('bridges.st.noAccess', 'no access'),
            'not-enabled': t('bridges.st.notEnabled', 'not enabled'),
            reserved: t('bridges.st.reserved', 'reserved'),
            excluded: t('bridges.excluded', 'excluded'),
        })[st] || t('bridges.st.unknown', 'not available');
    },

    _portKindText(a) {
        return BridgePorts.kind(a) === 'board'
            ? this.t('bridges.kindBoard', 'Board UART') : this.t('bridges.kindUsb', 'USB');
    },

    _portStatusHint(st) {
        const t = (k, d) => this.t(k, d);
        return ({
            'no-access': t('bridges.stHint.noAccess', 'The bridge container may not open this port yet. On the bridge’s host:'),
            'not-enabled': t('bridges.stHint.notEnabled', 'A UART of the board itself. It may already be in use — a GPS, a login console, a modem — and a program that does not lock it cannot be seen, so the bridge serves it only once you enable it.'),
            reserved: t('bridges.stHint.reserved', 'The system uses this port (kernel console or Bluetooth); the bridge never serves it.'),
            busy: t('bridges.stHint.busy', 'Another program holds this port. Stop it, or let that program keep the port.'),
            excluded: t('bridges.stHint.excluded', 'Listed in BRIDGE_EXCLUDE: another service owns it.'),
        })[st] || t('bridges.stHint.unknown', 'The bridge does not serve this port.');
    },

    // the ports a picker cannot offer, each with its reason and the fix to
    // copy; with a bridge record (its Buses dialog), a board UART that is
    // only waiting for an opt-in gets its Enable button
    _portBlockedHtml(list, bridgeId = null) {
        if (!list || !list.length) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const items = list.map(a => {
            const st = BridgePorts.status(a);
            const pill = (st === 'reserved' || st === 'excluded') ? 'off' : 'warn';
            return `<li class="br-blocked-item" data-status="${this._esc(st)}">
              <div><span class="sink-pill ${pill}">${this._esc(this._portStatusText(st))}</span>
                <b>${this._esc(this._portKindText(a))} · ${this._esc(BridgePorts.label(a))}</b>
                ${a.dev ? `<code>${this._esc(a.dev)}</code>` : ''}</div>
              <div class="field-hint">${this._esc(this._portStatusHint(st))}</div>
              ${a.fix ? `<div class="br-fix"><code class="br-fix-cmd">${this._esc(a.fix)}</code>
                <button type="button" class="btn btn-secondary btn-sm" ${this._act('copyText', [a.fix], { el: true })}
                  title="${this._esc(t('bridges.copyFix', 'Copy the command'))}" aria-label="${this._esc(t('bridges.copyFix', 'Copy the command'))}"><i aria-hidden="true" class="bi bi-clipboard"></i></button></div>` : ''}
              ${st === 'not-enabled' ? (bridgeId
                ? `<div class="br-fix"><button type="button" data-admin class="btn btn-secondary btn-sm" ${this._act('bridgeEnablePort', [bridgeId, a.key || a.stable_id, true])}>${this._esc(t('bridges.enable', 'Enable'))}</button></div>`
                : `<div class="field-hint">${this._esc(t('bridges.enableWhere', 'Enable it in the bridge’s Buses dialog (Devices → the bridge), or list it in BRIDGE_INCLUDE.'))}</div>`) : ''}
              ${a.reason && st !== 'not-enabled' ? `<div class="field-hint br-reason">${this._esc(a.reason)}</div>` : ''}
            </li>`;
        }).join('');
        return `<div class="br-blocked" role="group" aria-label="${this._esc(t('bridges.blockedTitle', 'Ports that cannot be used yet'))}">
            <div class="form-label">${this._esc(t('bridges.blockedTitle', 'Ports that cannot be used yet'))}</div>
            <ul class="br-blocked-list">${items}</ul></div>`;
    },

    // what the bridge warns about itself: a device-rule slot a kernel update
    // left stale (ADR-0016), with the command that rewrites it
    _bridgeWarningsHtml(warnings) {
        if (!warnings || !warnings.length) return '';
        const t = (k, d) => this.t(k, d);
        return `<div class="br-warnings" role="alert">${warnings.map(w => `<div class="br-blocked-item" data-warning>
            <div><span class="sink-pill warn">${this._esc(t('bridges.warning', 'warning'))}</span>
              <b>${this._esc(t('bridges.staleSlot', 'A device rule of the bridge is out of date'))}</b></div>
            <div class="field-hint">${this._esc(t('bridges.staleSlotHint', 'Usually after a kernel update: an RS-485 HAT got another device number. On the bridge’s host, run this again and recreate the container:'))}</div>
            ${w.fix ? `<div class="br-fix"><code class="br-fix-cmd">${this._esc(w.fix)}</code>
              <button type="button" class="btn btn-secondary btn-sm" ${this._act('copyText', [w.fix], { el: true })}
                title="${this._esc(t('bridges.copyFix', 'Copy the command'))}" aria-label="${this._esc(t('bridges.copyFix', 'Copy the command'))}"><i aria-hidden="true" class="bi bi-clipboard"></i></button></div>` : ''}
            <div class="field-hint br-reason">${this._esc(w.message || '')}</div></div>`).join('')}</div>`;
    },

    // buses the bridge guard keeps off the wire: the adapter a bus was bound
    // to is now on another port of its bridge, or gone. Never read from the
    // other bus; one click follows the adapter (bridge_guard.py)
    _guardBannerHtml(problems, devices) {
        if (!problems || !problems.length) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const refused = problems.filter(p => !p.allowed);
        const on = (p) => (devices || []).filter(d => String(d.host || d.connection?.host || '') === p.host
            && +(d.port || d.connection?.port) === +p.port).map(d => d.id);
        const hosts = [...new Set(problems.filter(p => p.status === 'moved').map(p => p.host))];
        const items = problems.map(p => {
            const ids = on(p);
            const what = p.status === 'moved'
                ? t('guard.moved', 'Bus {host}:{port} — its adapter {adapter} is now on :{now}. Not read until it follows.', { host: p.host, port: p.port, adapter: p.adapter, now: p.now_at })
                : p.status === 'missing'
                    ? t('guard.missing', 'Bus {host}:{port} — its adapter {adapter} is missing. Not read until it is back.', { host: p.host, port: p.port, adapter: p.adapter })
                    : t('guard.unverified', 'Bus {host}:{port} — the bridge cannot be asked which adapter serves it. Not verified, still reading as {adapter}.', { host: p.host, port: p.port, adapter: p.adapter });
            return `<li class="br-blocked-item" data-guard="${this._esc(p.status)}">
                <div><span class="sink-pill ${p.allowed ? 'warn' : 'bad'}">${this._esc(p.allowed ? t('guard.stillRead', 'not verified') : t('guard.notRead', 'not read'))}</span> <b>${this._esc(what)}</b></div>
                ${ids.length ? `<div class="field-hint">${this._esc(t('guard.devices', 'Devices: {ids}', { ids: ids.join(', ') }))}</div>` : ''}
                <div class="field-hint br-reason">${this._esc(p.detail || '')}</div></li>`;
        }).join('');
        const follow = hosts.map(h => `<button type="button" data-admin class="btn btn-primary btn-sm" ${this._act('bridgeGuardFollow', [h])}>
            <i aria-hidden="true" class="bi bi-signpost-split"></i> ${this._esc(t('guard.follow', 'Follow the adapters on {host}', { host: h }))}</button>`).join(' ');
        return `<div class="br-warnings" role="alert" id="guardBanner">
            <div class="form-label">${this._esc(refused.length ? t('guard.title', 'A bus moved on a serial bridge') : t('guard.titleWarn', 'A serial bridge cannot be checked'))}</div>
            <ul class="br-blocked-list">${items}</ul>
            ${follow ? `<div class="br-fix">${follow}</div>` : ''}</div>`;
    },

    async bridgeGuardFollow(host) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!await this.confirmAction({
            title: t('guard.followTitle', 'Follow the adapters?'),
            message: t('guard.followConfirm', 'Each device bound to a moved adapter is re-pointed to the port its adapter has now, and restarts there. Nothing is read from a bus it was not bound to.'),
            confirmLabel: t('guard.followGo', 'Follow'),
        })) return;
        const r = await fetch('/api/bridge-guard/follow', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ host }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast('error', t('guard.followFail', 'Not followed'), (d.detail?.errors || [r.statusText]).join(' ')); return; }
        const moves = Object.entries(d.moved || {}).map(([a, b]) => `:${a} → :${b}`).join(', ');
        this.showToast('success', t('guard.followed', 'Adapters followed'), moves);
        this.renderDevicesList();
    },

    // serve (or stop serving) one of the board's own UARTs: opt-in, with a
    // warning — the bridge cannot see a program that uses it without a lock
    async bridgeEnablePort(id, key, on) {
        const t = (k, d, p) => this.t(k, d, p);
        if (on && !await this.confirmAction({
            title: t('bridges.enableTitle', 'Serve this UART?'),
            message: t('bridges.enableWarn', 'Make sure nothing else uses it — a GPS, a login console, a modem, a device mapped straight into the gateway. Once served, anyone who can reach the bridge’s data port talks to whatever is on this line.'),
            confirmLabel: t('bridges.enable', 'Enable'),
        })) return;
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/ports/${encodeURIComponent(key)}/enable`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !!on }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast('error', t('bridges.enableFail', 'Not changed'), (d.detail?.errors || [r.statusText]).join(' ')); return; }
        this.showToast('success', on ? t('bridges.enabled', 'Port enabled') : t('bridges.disabled', 'Port no longer served'),
            `${d.label || key} · ${this._portStatusText(d.status)}`);
        this.bridgeScan(id);
    },

    // ── the Devices page section ────────────────────────────────────────────
    _bridgesSectionHtml(bridges, healthColor) {
        if (!bridges.length) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const bar = `<div class="bridges-bar"><span class="field-hint"><i aria-hidden="true" class="bi bi-hdd-network"></i> ${t('bridges.title', 'Bridges')}</span>
            <a class="btn btn-ghost btn-sm" href="/api/bridges/export" download><i aria-hidden="true" class="bi bi-download"></i> ${t('bridges.export', 'Export')}</a>
            <button data-admin class="btn btn-ghost btn-sm" ${this._act('openBridgesImport', [])}><i aria-hidden="true" class="bi bi-upload"></i> ${t('bridges.import', 'Import')}</button>
            <button data-admin class="btn btn-ghost btn-sm" ${this._act('openBridgesDiscover', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${t('bridges.find', 'Find on the LAN')}</button></div>`;
        const dot = { online: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                      offline: 'var(--danger,#ef4444)', unknown: 'var(--text-secondary)', checking: 'var(--text-secondary)' };
        return bridges.map(b => {
            const ty = this._bridgeType(b.type);
            const st = b.state || {};
            const ports = (b.ports || []).map(p => `
                <div class="bridge-port">
                  <div class="bridge-port-head">
                    <i aria-hidden="true" class="bi bi-diagram-2"></i>
                    <code>:${this._esc(p.port)}</code>
                    ${p.label ? `<span>${this._esc(p.label)}</span>` : ''}
                    ${p.serial_text || p.serial ? `<span class="dev-chip">${this._esc(p.serial_text || p.serial)}</span>` : ''}
                    <span class="field-hint">${this.tn('bridges.nDevices', (p.devices || []).length, '{n} devices')}</span>
                    ${p.busy ? this._busyMeter(p) : ''}
                    ${p.diag ? this._diagChip(b.id, p) : ''}
                    ${p.tapped ? `<span class="sink-pill ${p.wire?.error ? 'bad' : 'ok'}" title="${this._esc(p.wire?.error || t('bridges.tappedHint', 'Another master polls this bus; the gateway only listens. Scan, Check mode and polled devices are off here — they would transmit.'))}"><i aria-hidden="true" class="bi bi-ear"></i> ${p.wire?.error ? this._esc(t('bridges.tapLost', 'listening — link lost')) : this._esc(t('bridges.tapped', 'listening · {frames} frames · {crc} CRC errors', { frames: p.wire?.frames ?? 0, crc: p.wire?.crc_errors ?? 0 }))}</span>` : ''}
                    ${p.tapped ? '' : `<span class="bridge-port-actions">
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('busScan', [b.id, p.port])} title="${this._esc(t('bridges.scanBusHint', 'Ask every unit id on this bus who answers, and what it probably is'))}"><i aria-hidden="true" class="bi bi-search"></i> ${t('bridges.scanBus', 'Scan')}</button>
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('addDeviceOnBus', [b.id, p.port])} title="${this._esc(t('bridges.addDeviceHint', 'Add a device (a slave) on this bus'))}"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addDevice', 'Device')}</button>
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeProbe', [b.id, p.port])} title="${this._esc(t('bridges.probeHint', 'Ask a slave on this bus one register, as RTU and as Modbus TCP — tells whether the box is transparent or a gateway'))}"><i aria-hidden="true" class="bi bi-activity"></i> ${t('bridges.probe', 'Check mode')}</button>
                      <button class="btn btn-ghost btn-sm" ${this._act('openBusMonitor', [`${b.host}:${p.port}`])} title="${this._esc(t('bridges.monitorHint', 'Diagnostics, this bus only: every question and answer on the wire, and the register probe for any unit on it'))}"><i aria-hidden="true" class="bi bi-reception-4"></i> ${t('bridges.monitor', 'Monitor')}</button>
                    </span>`}
                  </div>
                  ${p.tapped ? `<div class="field-hint bridge-tap-why"><i aria-hidden="true" class="bi bi-ear"></i> ${this._esc(t('bridges.tappedHint', 'Another master polls this bus; the gateway only listens. Scan, Check mode and polled devices are off here — they would transmit.'))}</div>` : ''}
                  ${(p.devices || []).map(d => {
                    // the device's own line and ⋯ (the device page's, one builder —
                    // a unit's too); a unit opens its installation, which says how it is read
                    const full = (this._devices || []).find(x => x.id === d.id);
                    return `
                    <div class="bridge-dev" role="button" tabindex="0" data-f="${this._esc(d.id)}" ${this._act(d.endpoint_id ? 'openEndpointDetail' : 'openDeviceDetail', [d.endpoint_id || d.id], { guard: '.dev-row-menu' })}>
                      <span class="status-dot" style="--dot:${healthColor[d.health] || healthColor.idle}"></span>
                      <span class="dev-chip">${this._esc(this.t('common.unitN', 'unit {n}', { n: d.unit_id }))}</span>
                      ${d.tap ? `<span class="dev-chip" title="${this._esc(t('bridges.tapDevHint', 'listen-only'))}"><i aria-hidden="true" class="bi bi-ear"></i></span>` : ''}
                      <span>${this._esc(d.name || d.id)}</span>
                      ${full && !d.endpoint_id ? `<span class="field-hint bridge-dev-sum">${this._devSummaryHtml(full, { compact: true })}</span>` : ''}
                      ${d.endpoint_id ? `<span class="field-hint">${t('bridges.inInstallation', 'in an installation')}</span>` : ''}
                      ${full ? this._devRowMenuHtml(full, 'bridge') : ''}
                    </div>`; }).join('') || `<div class="field-hint bridge-dev">${t('bridges.noDevices', 'No device on this bus yet.')}</div>`}
                </div>`).join('');
            return `
            <div class="bridge-card" data-bridge="${this._esc(b.id)}">
              <div class="bridge-head">
                <i aria-hidden="true" class="bi bi-hdd-network"></i>
                <span class="status-dot" style="--dot:${dot[st.status] || dot.unknown}" title="${this._esc(st.detail || st.status || '')}"></span>
                <b>${this._esc(b.name || b.id)}</b>
                <span class="dev-chip">${this._esc(ty.name || b.type)}</span>
                <code>${this._esc(b.host)}</code>
                <span class="field-hint">${this._esc(t('bridges.state.' + (st.status || 'unknown'), st.status || 'unknown'))}${st.detail ? ' — ' + this._esc(st.detail) : ''}</span>
                <span class="bridge-actions">
                  ${ty.discovery === 'api' ? `<button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeScan', [b.id])}><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${t('bridges.scan', 'Buses')}</button>` : ''}
                  <button class="btn btn-ghost btn-sm" ${this._act('bridgeSetup', [b.id])}><i aria-hidden="true" class="bi bi-wrench"></i> ${t('bridges.setup', 'Set-up')}</button>
                  <button data-admin class="btn btn-ghost btn-sm" ${this._act('openBridgeModal', [b.id])} aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                  <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteBridge', [b.id])} aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </span>
              </div>
              ${ports || `<div class="field-hint bridge-dev">${ty.discovery === 'api'
                    ? t('bridges.noPortsApi', 'No bus seen yet — start the bridge on its host (Set-up), plug an adapter in, then Buses.')
                    : t('bridges.noPorts', 'No bus declared.')}</div>`}
            </div>`;
        }).join('').replace(/^/, bar);
    },

    // How busy the bus is: one measured value per bus (the wire's held time in
    // the last minute), never above 100 %; over capacity is said in words.
    // A meter for assistive tech, with the same words as the text beside it.
    _busyText(busy) {
        const t = (k, d, x) => this.t(k, d, x);
        return busy.over_capacity
            ? t('bridges.busyOver', 'over capacity')
            : t('bridges.busyText', 'busy {pct} % · capacity left ~{left} %',
                { pct: busy.pct, left: busy.capacity_left_pct ?? Math.max(0, 100 - busy.pct) });
    },

    _busyMeter(p) {
        const b = p.busy;
        const level = b.level === 'full' ? 'bad' : b.level === 'high' ? 'warn' : 'ok';
        const text = this._busyText(b);
        const pct = Math.max(0, Math.min(100, Number(b.pct) || 0));
        return `<span class="bus-meter ${level}" role="meter" aria-valuemin="0" aria-valuemax="100"
                  aria-valuenow="${pct}" aria-valuetext="${this._esc(text)}" tabindex="0"
                  aria-label="${this._esc(this.t('bridges.busyLabel', 'Bus :{port}', { port: p.port }))}"
                  title="${this._esc(this._busyTitle(p))}">
                  <span class="bus-meter-bar" aria-hidden="true"><span class="bus-meter-fill" style="width:${pct}%"></span></span>
                  <span class="bus-meter-text">${this._esc(text)}</span></span>`;
    },

    _busyTitle(p) {
        const t = (k, d, x) => this.t(k, d, x);
        const lines = (p.devices || []).filter(d => d.bus_share != null)
            .map(d => `${d.name || d.id}: ${Math.round(d.bus_share * 100)}%`);
        const advice = p.busy.level === 'full'
            ? t('bridges.busyFull', 'The bus cannot keep up: requests queue and readings age. Slow the poll groups that need no speed, raise the baud rate (every slave and the bus), or split the slaves over two buses.')
            : p.busy.level === 'high' ? t('bridges.busyHigh', 'Close to full: another device or a faster poll group will make readings late.') : '';
        return [t('bridges.busyLead', 'Measured share of the wire in the last minute, per device:'), ...lines, advice].filter(Boolean).join('\n');
    },

    // ── bus health: what the serial bridge measures on the line, next to what
    //    the gateway sees (bridge_diag.py, bridge-diag.js, ADR-0017) ─────────
    _diagText(code) {
        const t = (k, d) => this.t(k, d);
        return ({
            ok: t('bridges.diag.ok', 'line healthy'),
            line_noise: t('bridges.diag.lineNoise', 'line errors, reads still fine'),
            host_overrun: t('bridges.diag.hostOverrun', 'bridge host too busy'),
            wiring: t('bridges.diag.wiring', 'RS-485 wiring, termination or baud'),
            device_silent: t('bridges.diag.deviceSilent', 'device off or disconnected'),
            network: t('bridges.diag.network', 'network between the gateway and the bridge'),
            timeouts: t('bridges.diag.timeouts', 'a slave does not answer'),
            collecting: t('bridges.diag.collecting', 'measuring…'),
            no_data: t('bridges.diag.noData', 'bridge not answering'),
            unsupported: t('bridges.diag.none', 'no line statistics'),
        })[code] || t('bridges.diag.none', 'no line statistics');
    },

    _diagAdvice(code) {
        const t = (k, d) => this.t(k, d);
        return ({
            ok: t('bridges.diagHint.ok', 'No line errors, and the gateway’s reads are answered.'),
            line_noise: t('bridges.diagHint.lineNoise', 'The UART saw frame or parity errors, but every read was answered. Watch it: a loose terminal or a missing termination gets worse.'),
            host_overrun: t('bridges.diagHint.hostOverrun', 'The UART on the bridge’s host lost bytes (overrun): the host did not read them in time. Lighten its load, or use an adapter with a deeper buffer.'),
            wiring: t('bridges.diagHint.wiring', 'Timeouts or garbled answers together with frame or parity errors on the line: check A/B polarity, the 120 Ω termination at both ends, the ground, and that every device uses the same baud and parity.'),
            device_silent: t('bridges.diagHint.deviceSilent', 'The gateway asks and nothing at all comes back from the bus: the device is off, unplugged, or its cable is cut. The line itself shows no errors.'),
            network: t('bridges.diagHint.network', 'Timeouts while the line is clean and the TCP connection to the bridge keeps being reopened: look at the network between the gateway and the bridge (Wi-Fi, a switch, a firewall dropping idle connections).'),
            timeouts: t('bridges.diagHint.timeouts', 'Some reads time out while the line is clean and the bus answers otherwise: check that slave’s unit id, its settings and its response time.'),
            collecting: t('bridges.diagHint.collecting', 'The first numbers need two polls of the bridge (about a minute).'),
            no_data: t('bridges.diagHint.noData', 'The bridge’s control API does not answer the gateway: is it running, is its control port reachable, is the token right?'),
        })[code] || t('bridges.diagHint.none', 'An older bridge: update it to see line statistics.');
    },

    _diagPill(lvl) {
        return ({ ok: 'ok', warn: 'warn', info: 'info', unknown: 'off' })[lvl] || 'off';
    },

    _diagChip(bridgeId, p) {
        const t = (k, d, x) => this.t(k, d, x);
        const d = p.diag;
        const code = BridgeDiag.code(d);
        const text = this._diagText(code);
        const errs = BridgeDiag.lineErrors(d);
        const lpm = d.rates?.line_errors_per_min;
        const parts = [text,
            errs === null ? t('bridges.diag.lineUncounted', 'line errors not counted by this adapter')
                : t('bridges.diag.lineErrs', 'line errors: {n} ({rate}/min)', { n: errs, rate: lpm ?? '—' }),
            d.client ? t('bridges.diag.client', 'client {addr}', { addr: d.client }) : t('bridges.diag.noClient', 'no client connected'),
        ];
        return `<button type="button" class="sink-pill ${this._diagPill(BridgeDiag.level(d))} br-diag-chip" data-diag="${this._esc(code)}"
                  ${this._act('openBridgeHealth', [bridgeId, p.port])}
                  title="${this._esc(parts.join('\n'))}"
                  aria-label="${this._esc(t('bridges.diag.aria', 'Line health of bus :{port}: {text}', { port: p.port, text }))}">
                  <i aria-hidden="true" class="bi bi-heart-pulse"></i> ${this._esc(text)}</button>`;
    },

    _diagLineName(f) {
        const t = (k, d) => this.t(k, d);
        return ({ frame: t('bridges.diag.frame', 'Frame errors'), parity: t('bridges.diag.parity', 'Parity errors'),
                  overrun: t('bridges.diag.overrun', 'UART overruns'), brk: t('bridges.diag.brk', 'Breaks'),
                  buf_overrun: t('bridges.diag.bufOverrun', 'Buffer overruns') })[f] || f;
    },

    _diagEventText(kind) {
        const t = (k, d) => this.t(k, d);
        return ({
            client_connected: t('bridges.ev.clientConnected', 'client connected'),
            client_left: t('bridges.ev.clientLeft', 'client left'),
            line_errors: t('bridges.ev.lineErrors', 'line errors'),
            adapter_gone: t('bridges.ev.adapterGone', 'adapter gone'),
            adapter_back: t('bridges.ev.adapterBack', 'adapter back'),
            adapter_new: t('bridges.ev.adapterNew', 'adapter appeared'),
            port_moved: t('bridges.ev.portMoved', 'data port moved'),
            dev_renamed: t('bridges.ev.devRenamed', 'device node renamed'),
            status: t('bridges.ev.status', 'status changed'),
            ser2net_restart: t('bridges.ev.ser2netRestart', 'ser2net restarted'),
            ser2net_exited: t('bridges.ev.ser2netExited', 'ser2net stopped'),
            slot_warning: t('bridges.ev.slotWarning', 'device rule out of date'),
            started: t('bridges.ev.started', 'bridge started'),
        })[kind] || kind;
    },

    _secsText(s) {
        if (s === null || s === undefined) return '—';
        if (s < 120) return this.t('bridges.diag.secs', '{n} s', { n: Math.round(s) });
        if (s < 7200) return this.t('bridges.diag.mins', '{n} min', { n: Math.round(s / 60) });
        return this.t('bridges.diag.hours', '{n} h', { n: Math.round(s / 3600) });
    },

    _bridgeHealthHtml(port, v, now) {
        const t = (k, d, x) => this.t(k, d, x);
        if (!v) return `<p class="field-hint">${this._esc(t('bridges.diag.notYet', 'No numbers for this bus yet: the gateway asks the bridge every 30 s.'))}</p>`;
        const code = BridgeDiag.code(v);
        const st = v.stats || {};
        const act = BridgeDiag.activity(st, now);
        const ago = s => (s === null ? '—' : this._esc(t('bridges.diag.agoText', '{d} ago', { d: this._secsText(s) })));
        const rows = BridgeDiag.lineRows(st, v.rates).map(r => `<tr><th scope="row">${this._esc(this._diagLineName(r.field))}</th>
            <td>${r.known ? this._esc(r.total) : `<span class="field-hint">${this._esc(t('bridges.diag.notCounted', 'not counted'))}</span>`}</td>
            <td>${r.perMin === null ? '—' : this._esc(r.perMin)}</td></tr>`).join('');
        const m = v.mbg;
        const when = e => {
            const ms = BridgeDiag.eventTime(e);
            return ms === null ? '' : `<time datetime="${new Date(ms).toISOString()}">${this._esc(new Date(ms).toLocaleString(this._loc()))}</time>`;
        };
        const ev = (Array.isArray(v.events) ? v.events : []).filter(e => e && typeof e === 'object').slice().reverse().map(e => `<li>${when(e)}
            <b>${this._esc(this._diagEventText(e.kind))}</b> <span class="field-hint">${this._esc(e.message || '')}</span></li>`).join('');
        const dl = items => `<dl class="br-diag-dl">${items.map(([k, val]) => `<dt>${this._esc(k)}</dt><dd>${val}</dd>`).join('')}</dl>`;
        return `<section class="br-health" aria-label="${this._esc(t('bridges.diag.title', 'Line health · bus :{port}', { port }))}">
          <p role="status"><span class="sink-pill ${this._diagPill(BridgeDiag.level(v))}" data-diag="${this._esc(code)}">${this._esc(this._diagText(code))}</span>
            ${v.diagnosis?.confidence === 'low' ? `<span class="field-hint">${this._esc(t('bridges.diag.lowConfidence', '(this adapter does not count line errors, so the line cannot be ruled out)'))}</span>` : ''}</p>
          <p class="field-hint">${this._esc(this._diagAdvice(code))}</p>
          ${v.error ? `<p class="field-error">${this._esc(v.error)}</p>` : ''}
          <h4 class="tplu-h">${this._esc(t('bridges.diag.lineTitle', 'The line (kernel counters on the bridge)'))}</h4>
          ${st.line_note ? `<p class="field-hint">${this._esc(st.line_note)}</p>` : ''}
          <div class="br-table-wrap"><table class="tplu-table br-diag-table"><thead><tr><th scope="col">${this._esc(t('bridges.diag.counter', 'Counter'))}</th>
            <th scope="col">${this._esc(t('bridges.diag.total', 'Total'))}</th><th scope="col">${this._esc(t('bridges.diag.perMin', 'Per minute'))}</th></tr></thead>
            <tbody>${rows}</tbody></table></div>
          <h4 class="tplu-h">${this._esc(t('bridges.diag.activityTitle', 'Connection and activity'))}</h4>
          ${dl([
            [t('bridges.diag.clientLabel', 'Client'), act.client ? `<code>${this._esc(act.client)}</code>` : this._esc(t('bridges.diag.noClient', 'no client connected'))],
            [t('bridges.diag.reconnects', 'Connects in the last 10 min'), this._esc(st.connects_10min ?? '—')],
            [t('bridges.diag.silent', 'Bus silent for'), this._esc(this._secsText(act.silentS))],
            [t('bridges.diag.lastRx', 'Last byte from the bus'), ago(act.lastRxAgoS)],
            [t('bridges.diag.lastTx', 'Last byte to the bus'), ago(act.lastTxAgoS)],
          ])}
          <h4 class="tplu-h">${this._esc(t('bridges.diag.mbgTitle', 'What the gateway saw on this bus (last 5 min)'))}</h4>
          ${m ? dl([
            [t('bridges.diag.answered', 'Answered reads'), this._esc(m.ok ?? '—')],
            [t('bridges.diag.timeoutsLabel', 'Timeouts'), this._esc(m.timeouts ?? '—')],
            [t('bridges.diag.connErrors', 'Connection errors'), this._esc(m.connection ?? '—')],
            [t('bridges.diag.exceptions', 'Exceptions (the slave said no)'), this._esc(m.exceptions ?? '—')],
          ]) : `<p class="field-hint">${this._esc(t('bridges.diag.noMbg', 'No device of the gateway reads this bus.'))}</p>`}
          <h4 class="tplu-h">${this._esc(t('bridges.diag.eventsTitle', 'Recent events on the bridge'))}</h4>
          ${ev ? `<ul class="br-diag-events">${ev}</ul>` : `<p class="field-hint">${this._esc(t('bridges.diag.noEvents', 'None.'))}</p>`}
        </section>`;
    },

    async openBridgeHealth(bridgeId, port) {
        const t = (k, d, x) => this.t(k, d, x);
        const title = t('bridges.diag.title', 'Line health · bus :{port}', { port });
        let v = null, err = '';
        try {
            const r = await fetch(`/api/bridges/${encodeURIComponent(bridgeId)}/diagnostics`);
            const d = await r.json().catch(() => ({}));
            if (!r.ok) err = (d.detail?.errors || [r.statusText]).join(' ');
            else v = (d.ports || {})[String(port)] || null;
        } catch (e) { err = String(e.message || e); }
        this._bridgeInfo(title, err ? `<div class="field-error">${this._esc(err)}</div>`
            : this._bridgeHealthHtml(port, v, Date.now() / 1000));
    },

    // ── scan a bus ──────────────────────────────────────────────────────────
    async busScan(bridgeId, port) {
        const t = (k, d, x) => this.t(k, d, x);
        this._bridgeInfo(t('bridges.scanBusTitle', 'Scan bus :{port}', { port }), `
          <p class="field-hint">${t('bridges.scanBusLead', 'Asks unit ids 1–247 one by one through the bus\'s own connection — the devices on it keep reading in between. About a minute and a quarter for the whole range.')}</p>
          <div class="bus-scan-bar"><div id="busScanFill"></div></div>
          <p class="field-hint" id="busScanState">${t('bridges.scanStarting', 'starting…')}</p>
          <div id="busScanResult"></div>`);
        const ask = async body => {
            const r = await fetch(`/api/bridges/${encodeURIComponent(bridgeId)}/ports/${port}/scan`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            return { r, d: await r.json().catch(() => ({})) };
        };
        let { r, d } = await ask({});
        // nobody can say whether another master holds this port (a converter
        // that does not report its client): asked, never assumed (L3.7)
        if (r.status === 409 && !d.detail?.config_blocked && d.detail?.client === 'unknown') {
            const ok = await this.confirmAction({ title: t('am.confirmTitle', 'Open a connection to this bus?'),
                message: (d.detail.errors || [])[0] || '', confirmLabel: t('am.confirmOpen', 'Open it and scan'), danger: true });
            if (!ok) { document.getElementById('busScanState').textContent = t('am.notSent', 'Nothing was sent.'); return; }
            ({ r, d } = await ask({ confirm_open: true }));
        }
        if (!r.ok) { document.getElementById('busScanState').textContent = (d.detail?.errors || [r.statusText]).join(' '); return; }
        this._busScanJob = d.job;
        const tick = async () => {
            // the panel was closed or another scan started: stop this one on the
            // server too — a scan left running keeps asking the bus
            if (this._busScanJob !== d.job || !document.querySelector('#bridgeInfoModal.active #busScanFill')) {
                fetch(`/api/bus-scan/${d.job}`, { method: 'DELETE' }).catch(() => {});
                return;
            }
            const st = await (await fetch(`/api/bus-scan/${d.job}`)).json();
            document.getElementById('busScanFill').style.width = `${Math.round(100 * st.done / st.total)}%`;
            document.getElementById('busScanState').textContent = st.state === 'running'
                ? t('bridges.scanProgress', '{done}/{total} asked · {n} answered', { done: st.done, total: st.total, n: st.found.length })
                : st.state === 'failed' ? `${t('bridges.scanFailed', 'Scan failed')}: ${st.error}`
                : this.tn('bridges.scanDone', st.found.length, 'Done — {n} slaves answered.');
            document.getElementById('busScanResult').innerHTML = this._busScanTable(bridgeId, port, st.found);
            if (st.state === 'running') setTimeout(tick, 1000);
        };
        tick();
    },

    _busScanTable(bridgeId, port, found) {
        const t = (k, d, x) => this.t(k, d, x);
        if (!found.length) return '';
        return `<table class="tplu-table"><thead><tr><th>${t('bridges.colUnit', 'Unit')}</th><th>${t('bridges.scanWhat', 'What it is')}</th><th></th></tr></thead><tbody>
          ${found.map(f => {
              const what = f.matches.length ? f.matches.map(m => this._esc(m.name)).join(' / ')
                  : f.fc43 && (f.fc43.vendor || f.fc43.product) ? this._esc(`${f.fc43.vendor || ''} ${f.fc43.product || ''}`.trim())
                  : f.sunspec ? 'SunSpec' : `<span class="field-hint">${t('bridges.scanUnknown', 'not recognised — pick a template')}</span>`;
              const act = f.device ? `<span class="field-hint">${t('bridges.scanKnown', 'already: {id}', { id: this._esc(f.device) })}</span>`
                  : `<button data-admin class="btn btn-secondary btn-sm" ${this._act('addScannedDevice', [bridgeId, port, f.unit_id, f.suggested || ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.scanAdd', 'Add')}</button>`;
              return `<tr><td><code>${f.unit_id}</code></td><td>${what}</td><td>${act}</td></tr>`;
          }).join('')}</tbody></table>`;
    },

    addScannedDevice(bridgeId, port, unit, template) {
        if (this._busScanJob) fetch(`/api/bus-scan/${this._busScanJob}`, { method: 'DELETE' }).catch(() => {});
        this._busScanJob = null;
        this.closeModal('bridgeInfoModal');
        this.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: bridgeId, bridge_port: +port,
                                      unit_id: +unit, ...(template ? { template } : {}) });
    },

    // ── find bridges on the LAN ─────────────────────────────────────────────
    openBridgesDiscover() {
        const t = (k, d, x) => this.t(k, d, x);
        this.closeModal?.('bridgeModal');
        // a starting guess: the /24 of a bridge or a device we already know
        const ip = [...(this._bridges || []).map(b => b.host), ...(this._devices || []).map(d => d.host || d.connection?.host)]
            .find(h => /^\d+\.\d+\.\d+\.\d+$/.test(h || ''));
        this._bridgeInfo(t('bridges.findTitle', 'Find bridges on the LAN'), `
          <p class="field-hint">${t('bridges.findLead2', 'Looks at every address of the range for the ports each kind of bridge declares: our serial bridge answers its /health; a converter only has an open port, so one register is asked of a unit, as RTU and as Modbus TCP, to tell transparent from gateway. Bridges already added are not knocked on.')}</p>
          <div class="form-row align-end">
            <div class="form-group flex-2"><label class="form-label" for="brFindCidr">${t('bridges.findRange', 'Range')}</label>
              <input class="input" id="brFindCidr" value="${this._esc(ip ? ip.split('.').slice(0, 3).join('.') + '.0/24' : '')}" placeholder="192.168.1.0/24"></div>
            <div class="form-group"><label class="form-label" for="brFindUnit">${t('bridges.findUnit', 'Ask unit')}</label>
              <input class="input" id="brFindUnit" type="number" min="1" max="247" value="1"></div>
            <div class="form-group"><button data-admin class="btn btn-primary btn-sm" id="brFindBtn" ${this._act('bridgesDiscoverRun', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${t('bridges.findGo', 'Search')}</button></div>
          </div>
          <div id="brFindResult" role="status"></div>`);
    },

    async bridgesDiscoverRun() {
        const t = (k, d, x) => this.t(k, d, x);
        const box = document.getElementById('brFindResult');
        const btn = document.getElementById('brFindBtn');
        btn.disabled = true;
        box.innerHTML = `<div class="field-hint"><span class="btn-spinner"></span> ${t('bridges.finding', 'Searching… a /24 takes a few seconds.')}</div>`;
        try {
            const r = await fetch('/api/bridges/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cidr: document.getElementById('brFindCidr').value.trim(),
                                       unit_id: +document.getElementById('brFindUnit').value || 1 }) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error((d.detail?.errors || [d.detail || r.statusText]).join(' '));
            this._brFound = d.found || [];
            if (!this._brFound.length) {
                box.innerHTML = `<div class="field-hint">${this.tn('bridges.findNone', d.scanned, 'Nothing found in {n} addresses. A converter may use another port: add it by hand with the port from its web page.')}</div>`;
                return;
            }
            box.innerHTML = `<table class="tplu-table"><tbody>${this._brFound.map((x, i) => `<tr>
                <td><code>${this._esc(x.host)}${x.port ? ':' + this._esc(x.port) : ''}</code></td>
                <td>${x.bridge ? `<span class="sink-pill ok">${this._esc(t('bridges.findKnown', 'added as {id}', { id: x.bridge }))}</span>`
                    : x.type ? `<b>${this._esc(x.type_name || x.type)}</b>${x.version ? ' · ' + this._esc(x.version) : ''}`
                    : `<span class="sink-pill warn">${this._esc(t('bridges.findMaybe', 'a converter?'))}</span>`}</td>
                <td class="field-hint">${this._esc(x.detail || '')}</td>
                <td>${x.bridge ? '' : `<button data-admin class="btn btn-ghost btn-sm" ${this._act('addFoundBridge', [i])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.add', 'Add bridge')}</button>`}</td>
              </tr>`).join('')}</tbody></table>`;
        } catch (e) {
            box.innerHTML = `<div class="field-error">${this._esc(String(e.message || e))}</div>`;
        } finally {
            btn.disabled = false;
        }
    },

    addFoundBridge(i) {
        const x = (this._brFound || [])[i];
        if (!x) return;
        const type = x.type || (x.candidates || [])[0] || 'rtu_transparent';
        const ty = this._bridgeType(type);
        const preset = { type, host: x.host, name: x.host };
        if (ty.discovery === 'api') preset.control_port = x.port;
        else preset.ports = [{ port: x.port, label: '' }];
        this.closeModal('bridgeInfoModal');
        this.openBridgeModal(null, preset);
    },

    // ── import bridges ──────────────────────────────────────────────────────
    openBridgesImport() {
        const t = (k, d, x) => this.t(k, d, x);
        this._bridgeInfo(t('bridges.importTitle', 'Import bridges'), `
          <p class="field-hint">${t('bridges.importLead', 'A file exported from a gateway (Devices → Bridges → Export). Each bridge is checked first; tokens travel only in an admin\'s export.')}</p>
          <textarea id="brImportText" class="input" rows="9" spellcheck="false" style="font-family:var(--font-mono,monospace);font-size:12px;"></textarea>
          <label class="checkbox-label"><input type="checkbox" id="brImportReplace"> <span>${t('bridges.importReplace', 'Replace bridges that already exist here')}</span></label>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button data-admin class="btn btn-secondary btn-sm" ${this._act('bridgesImportRun', [false])}>${t('rules.import.check', 'Check')}</button>
            <button data-admin class="btn btn-primary btn-sm" ${this._act('bridgesImportRun', [true])}>${t('rules.import', 'Import')}</button></div>
          <div id="brImportResult" style="margin-top:10px;"></div>`);
    },

    async bridgesImportRun(apply) {
        const t = (k, d, x) => this.t(k, d, x);
        const r = await fetch('/api/bridges/import', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ yaml: document.getElementById('brImportText').value, apply,
                                   replace: document.getElementById('brImportReplace').checked }) });
        const d = await r.json().catch(() => ({}));
        const box = document.getElementById('brImportResult');
        if (!r.ok) { box.innerHTML = `<div class="field-error">${this._esc((d.detail?.errors || [r.statusText]).join(' '))}</div>`; return; }
        box.innerHTML = `<table class="tplu-table"><tbody>${d.bridges.map(x => `<tr><td><code>${this._esc(x.id)}</code> ${this._esc(x.name || '')}</td>
            <td><span class="sink-pill ${x.status === 'invalid' ? 'bad' : x.status === 'exists' ? 'warn' : 'ok'}">${this._esc(x.status)}</span></td>
            <td class="field-hint">${(x.errors || []).map(e => this._esc(e)).join('<br>')}</td></tr>`).join('')}</tbody></table>`;
        if (apply && d.ok) { this.showToast('success', t('bridges.imported', 'Bridges imported'), `${d.ok}/${d.total}`); this.renderDevicesList(); }
    },

    // ── add / edit ──────────────────────────────────────────────────────────
    async openBridgeModal(id = null, preset = null) {
        await this._loadBridges();
        const t = (k, d, p) => this.t(k, d, p);
        const b = id ? (this._bridges || []).find(x => x.id === id) : null;
        this._bridgeEdit = { id, data: b ? JSON.parse(JSON.stringify(b)) : {
            id: '', name: '', type: (this._bridgeTypes[0] || {}).id || 'rtu_transparent', host: '',
            ports: [], max_connections: 1, ...(preset || {}) } };
        document.getElementById('bridgeTitle').textContent = id
            ? t('bridges.editTitle', 'Edit bridge') : t('bridges.addTitle', 'Add a bridge');
        document.getElementById('bridgeFeedback').textContent = '';
        this._renderBridgeForm();
        this.openModal('bridgeModal');
    },

    _renderBridgeForm() {
        const t = (k, d, p) => this.t(k, d, p);
        const e = this._bridgeEdit, d = e.data;
        const ty = this._bridgeType(d.type);
        const manual = ty.discovery !== 'api';
        if (manual && !(d.ports || []).length) d.ports = [{ port: ty.default_port || 4196, label: '' }];
        document.getElementById('bridgeBody').innerHTML = `
          <div class="bridge-types" role="radiogroup">
            ${(this._bridgeTypes || []).map(x => `
              <label class="bridge-type ${x.id === d.type ? 'on' : ''}">
                <input type="radio" name="brType" value="${this._esc(x.id)}" ${x.id === d.type ? 'checked' : ''} ${e.id ? 'disabled' : ''}>
                <b>${this._esc(x.name)}</b>
                <span>${this._esc(x.description || '')}</span>
              </label>`).join('')}
          </div>
          ${e.id ? '' : `<div class="field-hint" style="margin:-4px 0 8px;"><a href="#" ${this._act('openBridgesDiscover', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${t('bridges.findLead', 'Not sure of its IP or port? Find bridges on the LAN')}</a></div>`}
          <div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="brName">${t('bridges.name', 'Name')}</label>
              <input class="input" id="brName" value="${this._esc(d.name || '')}" placeholder="${this._esc(t('bridges.namePh', 'Pi in the garage'))}"></div>
            <div class="form-group"><label class="form-label" for="brId">Id</label>
              <input class="input" id="brId" value="${this._esc(d.id || '')}" ${e.id ? 'disabled' : ''} placeholder="pi-garage"></div>
            <div class="form-group"><label class="form-label" for="brHost">${t('bridges.host', 'IP / host')}</label>
              <input class="input" id="brHost" value="${this._esc(d.host || '')}" placeholder="192.168.1.50"></div>
          </div>
          ${ty.framing === 'modbus_tcp' ? `
          <div class="form-row"><div class="form-group"><label class="form-label" for="brMax">${t('bridges.maxConn', 'Connections it accepts at once')}</label>
            <input class="input" id="brMax" type="number" min="1" max="16" value="${d.max_connections || 1}">
            <div class="field-hint">${t('bridges.maxConnHint', 'With multi-host on, several systems can read it; the gateway opens this many.')}</div></div></div>` : ''}
          ${ty.discovery === 'api' ? `
          <div class="form-row"><div class="form-group"><label class="form-label" for="brCtrl">${t('bridges.ctrlPort', 'Control port')}</label>
            <input class="input" id="brCtrl" type="number" value="${d.control_port || ty.control_port || 7000}"></div></div>
          <div class="field-hint">${t('bridges.apiHint', 'Its buses are read from the bridge itself; after saving, Set-up gives the command to start it on its host, with its token.')}</div>` : `
          <div class="form-group"><label class="form-label">${t('bridges.ports', 'Buses on this box (TCP ports)')}</label>
            ${(d.ports || []).map((p, i) => `
              <div class="form-row bridge-port-row">
                <input class="input br-port" data-i="${i}" type="number" min="1" max="65535" value="${this._esc(p.port || '')}" aria-label="${this._esc(this.t('bridges.tcpPort', 'TCP port'))}">
                <input class="input br-label" data-i="${i}" value="${this._esc(p.label || '')}" placeholder="${this._esc(t('bridges.portLabel', 'label, e.g. panel A'))}">
                <input class="input br-serial" data-i="${i}" value="${this._esc(p.serial || '')}" placeholder="9600 8N1" title="${this._esc(t('bridges.serialNote', 'What the converter is set to — for your reference; set it in its web page'))}">
                <button class="btn btn-ghost btn-sm" ${this._act('bridgeDelPort', [i])} aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-x-lg"></i></button>
              </div>`).join('')}
            ${(d.ports || []).length < (ty.max_ports || 4) ? `<button class="btn btn-ghost btn-sm" ${this._act('bridgeAddPort', [])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addPort', 'Bus')}</button>` : ''}
          </div>`}
          <details class="tpl-proto" open><summary><i aria-hidden="true" class="bi bi-wrench"></i> ${t('bridges.onTheBox', 'What to set on the box')}</summary>
            <ul class="tplu-list">${(ty.setup || []).map(s => `<li>${this._esc(s)}</li>`).join('')}</ul></details>`;
        document.querySelectorAll('#bridgeBody input[name="brType"]').forEach(r => r.addEventListener('change', () => {
            this._bridgeCollect(); d.type = r.value; d.ports = []; this._renderBridgeForm(); }));
        const n = document.getElementById('brName'), idf = document.getElementById('brId');
        if (n && idf && !e.id) n.addEventListener('input', () => {
            if (!idf.dataset.touched) idf.value = n.value.toLowerCase().normalize('NFD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
        });
        idf?.addEventListener('input', () => { idf.dataset.touched = '1'; });
    },

    _bridgeCollect() {
        const d = this._bridgeEdit.data, g = id => document.getElementById(id);
        d.name = g('brName')?.value.trim() ?? d.name;
        if (!this._bridgeEdit.id) d.id = g('brId')?.value.trim() ?? d.id;
        d.host = g('brHost')?.value.trim() ?? d.host;
        if (g('brMax')) d.max_connections = parseInt(g('brMax').value, 10) || 1;
        if (g('brCtrl')) d.control_port = parseInt(g('brCtrl').value, 10) || 7000;
        document.querySelectorAll('#bridgeBody .br-port').forEach(inp => {
            const i = +inp.dataset.i;
            d.ports[i] = { ...d.ports[i], port: parseInt(inp.value, 10) || null,
                           label: document.querySelector(`#bridgeBody .br-label[data-i="${i}"]`)?.value.trim() || '',
                           serial: document.querySelector(`#bridgeBody .br-serial[data-i="${i}"]`)?.value.trim() || '' };
        });
    },

    bridgeAddPort() {
        this._bridgeCollect();
        const d = this._bridgeEdit.data;
        const next = Math.max(0, ...(d.ports || []).map(p => p.port || 0)) + 1;
        d.ports.push({ port: next > 1 ? next : (this._bridgeType(d.type).default_port || 4196), label: '' });
        this._renderBridgeForm();
    },

    bridgeDelPort(i) {
        this._bridgeCollect();
        this._bridgeEdit.data.ports.splice(i, 1);
        this._renderBridgeForm();
    },

    async saveBridge() {
        const t = (k, d, p) => this.t(k, d, p);
        this._bridgeCollect();
        const e = this._bridgeEdit, d = e.data;
        const body = { id: d.id, name: d.name, type: d.type, host: d.host,
                       max_connections: d.max_connections || 1,
                       ports: (d.ports || []).filter(p => p.port).map(({ devices, ...p }) => p) };
        if (d.control_port) body.control_port = d.control_port;
        const fb = document.getElementById('bridgeFeedback');
        try {
            const r = await fetch(e.id ? `/api/bridges/${encodeURIComponent(e.id)}` : '/api/bridges', {
                method: e.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body) });
            const res = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error((res.detail?.errors || [r.statusText]).join('\n'));
            this.closeModal('bridgeModal');
            this.showToast('success', t('bridges.saved', 'Bridge saved'),
                (res.restarted || []).length ? t('bridges.restarted', 'its devices reconnected: {list}', { list: res.restarted.join(', ') }) : res.name || res.id);
            await this.renderDevicesList();
            if (!e.id && this._bridgeType(d.type).discovery === 'api') this.bridgeSetup(res.id);
        } catch (err) {
            fb.textContent = String(err.message || err); fb.className = 'save-feedback err';
        }
    },

    async deleteBridge(id) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!await this.confirmAction({
            title: t('bridges.deleteTitle', 'Delete bridge {id}', { id }),
            message: t('bridges.deleteConfirm', 'Only the bridge is removed; a bridge that still carries devices is refused.'),
            confirmLabel: t('common.delete', 'Delete'), danger: true,
        })) return;
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast('error', t('bridges.deleteFail', 'Cannot delete'), (d.detail?.errors || [r.statusText]).join(' ')); return; }
        this.renderDevicesList();
    },

    // ── information panels: set-up, buses, mode check ───────────────────────
    _bridgeInfo(title, html) {
        document.getElementById('bridgeInfoTitle').textContent = title;
        document.getElementById('bridgeInfoBody').innerHTML = html;
        this.openModal('bridgeInfoModal');
    },

    async bridgeSetup(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const s = await (await fetch(`/api/bridges/${encodeURIComponent(id)}/setup`)).json();
        const steps = `<ol class="tplu-list">${(s.steps || []).map(x => `<li>${this._esc(x)}</li>`).join('')}</ol>`;
        const run = s.docker_run ? `
          <h4 class="tplu-h">${t('bridges.runOnHost', 'On the host the adapters are plugged into')}</h4>
          <p class="field-hint">${t('bridges.runHint', 'Any Linux with Docker — a Raspberry Pi, a server. Copy, run, and the bridge shows online here within a few seconds.')}</p>
          <pre class="code-block" id="brRun">${this._esc(s.docker_run)}</pre>
          <button class="btn btn-secondary btn-sm" ${this._act('copyElText', ['brRun'])}><i aria-hidden="true" class="bi bi-clipboard"></i> ${t('common.copy', 'Copy')}</button>
          <p class="field-hint" style="margin-top:8px;">${t('bridges.composeHint', 'Prefer compose: serial-bridge/docker-compose.bridge.yml, with BRIDGE_TOKEN set to the token in the command.')}
            <a href="https://github.com/sm26449/multi-bus-gateway/blob/main/docs/rtu-over-network.md" target="_blank" rel="noopener">${t('bridges.guide', 'Guide')}</a></p>` : '';
        this._bridgeInfo(t('bridges.setupTitle', 'Set-up'), run + `<h4 class="tplu-h">${t('bridges.onTheBox', 'What to set on the box')}</h4>` + steps);
    },

    // copy an element's text (copyText in app-devices copies a given string)
    copyElText(elId) {
        const txt = document.getElementById(elId)?.textContent || '';
        navigator.clipboard?.writeText(txt).then(() => this.showToast('success', this.t('common.copied', 'Copied'), ''));
    },

    async bridgeScan(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/scan`);
        const s = await r.json().catch(() => ({}));
        if (!r.ok) { this._bridgeInfo(t('bridges.scanTitle', 'Buses'), `<div class="field-error">${this._esc((s.detail?.errors || [r.statusText]).join(' '))}</div>`); return; }
        const PAR = ['N', 'E', 'O'], BAUD = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200];
        // the buses it serves are rows; a port it cannot serve (a HAT channel
        // the container may not open yet, the console) is listed under them
        const { usable, blocked } = BridgePorts.split(s.ports || []);
        const rows = usable.map((p, i) => {
            const sp = p.serial_params || {};
            const board = BridgePorts.kind(p) === 'board';
            return `<tr data-key="${this._esc(p.key)}">
              <td style="white-space:nowrap;"><code>:${this._esc(p.port)}</code></td><td><span class="sink-pill off">${this._esc(this._portKindText(p))}</span> ${this._esc(BridgePorts.label(p))}${p.dev ? ` <code>${this._esc(p.dev)}</code>` : ''}</td>
              <td><select class="input input-sm br-baud" data-i="${i}" aria-label="${this._esc(t('bridges.colBaud', 'Baud'))}">${BAUD.map(b => `<option ${b === sp.baud ? 'selected' : ''}>${b}</option>`).join('')}</select></td>
              <td><select class="input input-sm br-par" data-i="${i}" aria-label="${this._esc(t('bridges.parity', 'Parity'))}">${PAR.map(x => `<option ${x === sp.parity ? 'selected' : ''}>${x}</option>`).join('')}</select></td>
              <td><select class="input input-sm br-stop" data-i="${i}" aria-label="${this._esc(t('bridges.colStop', 'Stop'))}">${[1, 2].map(x => `<option ${x === sp.stopbits ? 'selected' : ''}>${x}</option>`).join('')}</select></td>
              <td>${board ? `<input type="checkbox" class="br-rs485" data-i="${i}" ${p.rs485 ? 'checked' : ''} aria-label="${this._esc(t('bridges.rs485Aria', 'RS-485 direction driven by the kernel (RTS)'))}" title="${this._esc(t('bridges.rs485Hint', 'Turn on only for a board whose RS-485 chip does not switch direction by itself'))}">` : '—'}</td>
              <td>${p.diag ? this._diagChip(id, p) : '—'}</td>
              <td style="white-space:nowrap;"><button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeSetSerial', [id, p.key, i])}>${t('common.save', 'Save')}</button>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('addDeviceOnBus', [id, p.port])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addDevice', 'Device')}</button>${p.needs_enable ? `
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeEnablePort', [id, p.key, false])}>${t('bridges.disable', 'Disable')}</button>` : ''}</td></tr>`;
        }).join('');
        this._scanPorts = usable;
        this._bridgeInfo(t('bridges.scanTitle', 'Buses'), `
          <p class="field-hint">${this._esc(t('bridges.scanLead', '{host} · bridge {version} — one bus per adapter; serial settings apply to that bus only.', { host: s.hostname || id, version: s.version || '' }))}</p>
          ${this._bridgeWarningsHtml(s.warnings)}
          ${rows ? `<table class="tplu-table"><thead><tr><th>${t('bridges.colPort', 'Port')}</th><th>${t('bridges.adapter', 'Adapter')}</th><th>${t('bridges.colBaud', 'Baud')}</th><th>${t('bridges.parity', 'Parity')}</th><th>${t('bridges.colStop', 'Stop')}</th><th style="white-space:nowrap;">RS-485</th><th>${t('bridges.diag.health', 'Line health')}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
                 : `<p>${t('bridges.noAdapters', 'No adapter plugged in on that host.')}</p>`}
          ${this._portBlockedHtml(blocked, id)}`);
    },

    async bridgeSetSerial(id, key, i) {
        const t = (k, d, p) => this.t(k, d, p);
        const q = c => document.querySelector(`#bridgeInfoBody .${c}[data-i="${i}"]`)?.value;
        const body = { baud: +q('br-baud'), parity: q('br-par'), stopbits: +q('br-stop'), databits: 8 };
        const rs = document.querySelector(`#bridgeInfoBody .br-rs485[data-i="${i}"]`);
        if (rs) body.rs485 = rs.checked;                 // board UARTs only
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/ports/${encodeURIComponent(key)}/serial`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast('error', t('bridges.serialFail', 'Not applied'), (d.detail?.errors || [r.statusText]).join(' ')); return; }
        this.showToast('success', t('bridges.serialDone', 'Bus settings applied'), d.serial_text || '');
        this.renderDevicesList();
    },

    async bridgeProbe(id, port) {
        const t = (k, d, p) => this.t(k, d, p);
        const unit = await this.askValue({
            title: t('bridges.probeTitle', 'Check mode'),
            label: t('bridges.probeUnit', 'Unit id of a slave on this bus:'),
            min: 1, max: 247, step: 1, initial: 1,
            confirmLabel: t('bridges.probeGo', 'Check'),
        });
        if (unit === null) return;
        this._bridgeInfo(t('bridges.probeTitle', 'Check mode'), `<p class="field-hint">${t('bridges.probing', 'Asking… (a converter that takes one client drops the gateway\'s connection for a moment)')}</p>`);
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/probe`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ port, unit_id: unit }) });
        const d = await r.json().catch(() => ({}));
        document.getElementById('bridgeInfoBody').innerHTML = `
          <p><b>${this._esc(d.verdict || d.error || (d.detail?.errors || []).join(' ') || r.statusText)}</b></p>
          <table class="tplu-table"><tbody>
            <tr><td>RTU</td><td>${this._esc(d.rtu || '—')}</td></tr>
            <tr><td>Modbus TCP</td><td>${this._esc(d.modbus_tcp || '—')}</td></tr>
          </tbody></table>`;
    },

    addDeviceOnBus(bridgeId, port) {
        this.closeModal?.('bridgeInfoModal');
        const used = ((this._bridges || []).find(b => b.id === bridgeId)?.ports || [])
            .find(p => +p.port === +port)?.devices?.map(d => +d.unit_id) || [];
        let unit = 1; while (used.includes(unit)) unit++;
        this.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: bridgeId, bridge_port: +port, unit_id: unit });   // a gateway's bus opens as TCP
    },
});
