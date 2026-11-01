/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The connection step's choices (ADR-0031 §2, design §3 Step 2, L3.6): which
// answers each question may take, and why one may not. Pure: no DOM, no
// fetch — the component (conn-step.js) draws what this says, Node tests it
// (tools/e2e/conn_step_model_test.mjs).
//
// The questions, in order: 2a port → 2b where the bus is (reach) → 2c role on
// the bus (Master / Listen-only) → 2d protocol → 2e line → 2f address; 2g CAN,
// 2h HTTP, 2i MQTT. Every option comes from what the server says — the matrix
// and its invalid cells (GET /api/protocols), the buses (GET /api/buses), the
// template summary (speaks, ports, listen_only, line, default_unit_id,
// http_path, master_hint) and a saved device's locks — never from a rule
// written here. An option that cannot be taken stays listed, off, with its
// reason (the component makes it aria-disabled, focusable, its reason beside).
//
// What a choice is stored as stays ConnformModel's (connform-model.js): this
// moves its M between cells (applyAxes) and reads it back (axesOf); the
// payload is ConnformModel.payload, byte-identical for the same choices.
//
//   axesOf(M) / applyAxes(M, axes)     M ↔ {port, reach, protocol, framing, udp, mode}
//   options(ctx, t)                    {ports, reaches, protocols, roles, prefill, forced, hint}
//   lineInfo(ctx, t), addressInfo(ctx, t)
//   canGate(iface, name, bitrate, t), canCommand(name, bitrate)
//   httpUrl(host, id, path), httpParse(url, path)
//   busFor(M, buses, serialPorts)      the known bus (GET /api/buses row) M is on
//   pickerRole(axes, role)             what the serial picker judges ports for
//   roleAfterBusChange(axes, role, options)
//   errors(ctx, t)                     what blocks Next / Save before the server
//
// ctx: { protocols: {matrix: {device}, invalid: {device}}, axes, role
//        ('poll'|'listen'|null), context ('add'|'edit'), tpl (template
//        summary), locks (the device's), orig (its stored axes), bus (its
//        /api/buses row), selfId, bridge ({id, name, api}), can (an
//        /api/can-interfaces row) }
(function (root) {
    'use strict';

    const PORTS = ['serial', 'ethernet', 'can', 'http', 'mqtt'];
    const REACHES = ['here', 'bridge', 'address', 'socketcan'];
    const ROLES = ['poll', 'listen'];
    const AXES = ['port', 'reach', 'protocol', 'framing', 'udp', 'mode'];
    const DEFAULT_LINE = { baudrate: 9600, parity: 'N', bytesize: 8, stopbits: 1 };
    const fill = (s, p) => String(s).replace(/\{(\w+)\}/g, (m, k) => (p && k in p ? String(p[k]) : m));
    const T0 = (k, d, p) => fill(d, p);
    const clone = M => { const o = JSON.parse(JSON.stringify(M || {})); o.keepalive = (M || {}).keepalive; return o; };
    const ok = value => ({ value, state: 'ok', code: '', reason: '' });
    const off = (value, code, reason) => ({ value, state: 'off', code, reason });
    const cellsOf = ctx => ((ctx.protocols || {}).matrix || {}).device || [];
    const invalidOf = ctx => ((ctx.protocols || {}).invalid || {}).device || [];
    const tplName = tpl => (tpl && (tpl.name || tpl.id)) || '';
    const lockOf = (ctx, field) => (ctx.locks || []).find(l => l.field === field) || null;
    // a lock in the page's words (lock-text.js, L3.10-F16) when it is loaded
    // and a dictionary is given; else the server's, without its field
    const lockText = (l, t) => {
        if (t && t !== T0 && root.LockText) return root.LockText.text(l, t);
        const s = String(l.text || ''); return s.startsWith(`${l.field}: `) ? s.slice(l.field.length + 2) : s;
    };
    const lineText = l => (l ? `${l.baudrate ?? l.baud} ${l.bytesize}${l.parity}${l.stopbits}` : '');
    const fromBus = l => (l ? { baudrate: l.baud ?? l.baudrate, parity: l.parity, bytesize: l.bytesize, stopbits: l.stopbits } : null);

    // ── M ↔ axes ──────────────────────────────────────────────────────────
    function axesOf(M) {
        M = M || {};
        const ax = (port, reach, protocol, framing, udp, mode) => ({ port, reach, protocol, framing, udp: !!udp, mode });
        const bridged = !!M.bridge;
        switch (M.mode) {
        case 'none': return ax(null, null, null, null, false, null);
        case 'tcp': return bridged ? ax('ethernet', 'bridge', 'modbus', 'mbap', false, 'poll')
            : ax('ethernet', 'address', 'modbus', 'mbap', M.udp, 'poll');
        case 'rtu': return ax('serial', 'here', 'modbus', M.framing === 'ascii' ? 'ascii' : 'rtu', false, 'poll');
        case 'rtu-tcp': return ax('serial', bridged ? 'bridge' : 'address', 'modbus', M.framing === 'ascii' ? 'ascii' : 'rtu', false, 'poll');
        case 'rtu_tap': return ax('serial', bridged ? 'bridge' : 'here', 'modbus', 'rtu', false, 'listen');
        case 'seplos_v2': return ax('serial', M.net ? 'address' : 'here', 'seplos_v2', null, false,
            ROLES.includes(M.bus_mode) ? M.bus_mode : null);
        case 'can': return ax('can', 'socketcan', 'can_signals', null, false, 'listen');
        case 'http': return ax('http', null, 'json_http', null, false, 'poll');
        case 'mqtt': return ax('mqtt', null, 'json_mqtt', null, false, 'listen');
        default: return ax(null, null, null, null, false, null);
        }
    }

    // M moved to the cell the axes name; what is not the cell's (an address,
    // a unit, a line) is kept. A bridge is kept only where the cell is
    // reached through one (the component fits it to the bridge's kind).
    function applyAxes(M, ax) {
        const out = clone(M);
        const dropBridge = () => { out.bridge = ''; out.bridge_port = null; };
        if (!ax || (ax.port == null && ax.protocol == null)) { out.mode = 'none'; return out; }
        const listen = ax.mode === 'listen';
        switch (ax.protocol) {
        case 'modbus':
            if (ax.port === 'ethernet') {
                out.mode = 'tcp';
                if (ax.reach === 'bridge') out.udp = false;
                else { dropBridge(); out.udp = !!ax.udp; if (out.port == null) out.port = 502; }
            } else if (ax.reach === 'here') {
                out.mode = listen ? 'rtu_tap' : 'rtu';
                dropBridge();
            } else if (ax.reach === 'bridge') {
                out.mode = listen ? 'rtu_tap' : 'rtu-tcp';
                out.legacy = false;
            } else {
                out.mode = 'rtu-tcp';                // a converter's address (listening there: not offered)
                out.legacy = true;
                dropBridge();
            }
            if (ax.port === 'serial') out.framing = listen ? 'rtu' : (ax.framing === 'ascii' ? 'ascii' : 'rtu');
            break;
        case 'seplos_v2':
            out.mode = 'seplos_v2';
            out.net = ax.reach === 'address';
            out.bus_mode = ROLES.includes(ax.mode) ? ax.mode : '';
            dropBridge();
            break;
        case 'can_signals': out.mode = 'can'; dropBridge(); break;
        case 'json_http': out.mode = 'http'; dropBridge(); break;
        case 'json_mqtt': out.mode = 'mqtt'; dropBridge(); break;
        default: break;
        }
        return out;
    }

    // ── what fits: the template, a saved device's class ───────────────────
    // the cells a device may take here, each with why not when it may not
    // a saved device's class lock (its words come first: they say why for
    // this device), then the template's protocol
    function lockFitOf(ctx, t) {
        const cls = lockOf(ctx, 'connection.protocol');
        const orig = ctx.orig || null;
        if (!cls || !orig) return () => null;
        // the lock codes are the server's (device_summary.locks); which of them
        // hold the whole connection, and which decide a role, is written here
        // until the server serves it with each lock (FINDINGS L3.6-R1)
        const held = ['components', 'primary', 'meter_only'].includes(cls.code);
        return c => ((held && (c.port !== orig.port || c.protocol !== orig.protocol)) || c.protocol !== orig.protocol
            ? { code: cls.code, reason: lockText(cls, t) } : null);
    }
    function fitOf(ctx, t) {
        const tpl = ctx.tpl || null;
        const lockFit = lockFitOf(ctx, t);
        return c => {
            const locked = lockFit(c);
            if (locked) return locked;
            if (tpl && tpl.speaks && c.protocol !== tpl.speaks) {
                return { code: 'tpl_speaks', reason: t('cs.why.tplSpeaks', '{model} speaks {proto}: its map is read by that protocol only.',
                    { model: tplName(tpl), proto: protoWord(tpl.speaks) }) };
            }
            return null;
        };
    }
    const PROTO_WORDS = { modbus: 'Modbus', seplos_v2: 'Seplos V2', can_signals: 'CAN', json_http: 'HTTP / JSON', json_mqtt: 'MQTT / JSON' };
    const protoWord = p => PROTO_WORDS[p] || String(p || '');
    const PORT_WORDS = { serial: 'RS-485 / serial', ethernet: 'Ethernet', can: 'CAN', http: 'Web API (HTTP)', mqtt: 'MQTT' };

    // the first refusal among cells, or null when one of them is open
    function judge(cells, fit, t) {
        if (!cells.length) return { code: 'not_offered', reason: t('cs.why.notOffered', 'Not offered for this connection.') };
        // a cell that fits but is not available yet says its own note first
        let notYet = null, first = null;
        for (const c of cells) {
            const why = fit(c);
            if (!why && c.status === 'today') return null;
            if (!why && !notYet) notYet = { code: c.status, reason: c.note || t('cs.why.notYet', 'Not available yet.') };
            if (why && !first) first = why;
        }
        return notYet || first;
    }

    function portOptions(ctx, t) {
        const fit = fitOf(ctx, t);
        const tpl = ctx.tpl || null;
        const cells = cellsOf(ctx);
        const lockFit = lockFitOf(ctx, t);
        return PORTS.filter(p => cells.some(c => c.port === p)).map(p => {
            const here = cells.filter(c => c.port === p);
            if (here.every(c => lockFit(c))) { const w = lockFit(here[0]); return off(p, w.code, w.reason); }
            if (tpl && Array.isArray(tpl.ports) && tpl.ports.length && !tpl.ports.includes(p)) {
                return off(p, 'tpl_port', t('cs.why.tplPort', '{model} is not reached over {port}.', { model: tplName(tpl), port: PORT_WORDS[p] }));
            }
            const why = judge(cells.filter(c => c.port === p), fit, t);
            if (why && why.code === 'tpl_speaks') {
                return off(p, 'tpl_port', t('cs.why.tplPort', '{model} is not reached over {port}.', { model: tplName(tpl), port: PORT_WORDS[p] }));
            }
            return why ? off(p, why.code, why.reason) : ok(p);
        });
    }

    function reachOptions(ctx, t) {
        const ax = ctx.axes || {};
        const fit = fitOf(ctx, t);
        const cells = cellsOf(ctx).filter(c => c.port === ax.port && c.reach);
        const orig = ctx.orig || null;
        const cls = lockOf(ctx, 'connection.protocol');
        return REACHES.filter(r => cells.some(c => c.reach === r)).map(r => {
            if (cls && orig && cls.code === 'components' && r !== orig.reach) return off(r, cls.code, lockText(cls, t));
            const why = judge(cells.filter(c => c.reach === r), fit, t);
            return why ? off(r, why.code, why.reason) : ok(r);
        });
    }

    // 2d: a serial bus here or at an address speaks Modbus RTU, Modbus ASCII
    // or Seplos V2; through a bridge the bridge's kind says the framing
    function protocolKey(c) {
        if (c.protocol !== 'modbus') return c.protocol;
        if (c.port === 'serial' && c.reach !== 'bridge') return `modbus:${c.framing}`;
        return 'modbus';
    }
    function protocolOptions(ctx, t) {
        const ax = ctx.axes || {};
        const fit = fitOf(ctx, t);
        const cells = cellsOf(ctx).filter(c => c.port === ax.port && c.reach === ax.reach);
        const keys = [];
        for (const c of cells) if (!keys.includes(protocolKey(c))) keys.push(protocolKey(c));
        return keys.map(k => {
            const why = judge(cells.filter(c => protocolKey(c) === k), fit, t);
            return why ? off(k, why.code, why.reason) : ok(k);
        });
    }

    // 2c: Master / Listen-only, each from the matrix, its invalid cells, the
    // template and the bus's claims (the device being edited is not its own)
    function claimsOf(ctx) {
        const bus = ctx.bus || null;
        const self = ctx.selfId || null;
        if (!bus) return { listeners: [], pollers: [] };
        // said by name (the id when it has none)
        const name = id => ((bus.devices || []).find(d => d.id === id) || {}).name || id;
        const others = list => (list || []).filter(id => id !== self).map(name);
        return { listeners: others(bus.listeners), pollers: others(bus.pollers) };
    }
    function roleOptions(ctx, t) {
        const ax = ctx.axes || {};
        const tpl = ctx.tpl || null;
        if (!ax.port || ax.port === 'http' || ax.port === 'mqtt') return [];
        const same = c => c.port === ax.port && c.reach === ax.reach && c.protocol === ax.protocol
            && (ax.protocol !== 'modbus' || c.framing === ax.framing)
            && (ax.port !== 'ethernet' || !!c.udp === !!ax.udp);
        const { listeners, pollers } = claimsOf(ctx);
        const roleLock = ctx.context === 'edit' && ctx.sameBus !== false ? lockOf(ctx, 'connection.role') : null;
        return ROLES.map(mode => {
            const inv = invalidOf(ctx).find(c => same(c) && c.mode === mode);
            // the server's reason, in the page's words (L3.11c; the English
            // is the fallback)
            if (inv) return off(mode, inv.code, t(`cs.inv.${inv.code}`, inv.text));
            const cell = cellsOf(ctx).find(c => same(c) && c.mode === mode);
            if (!cell) return off(mode, 'not_offered', t('cs.why.notOffered', 'Not offered for this connection.'));
            if (cell.status !== 'today') return off(mode, cell.status, cell.note || t('cs.why.notYet', 'Not available yet.'));
            if (mode === 'poll' && tpl && tpl.listen_only) {
                return off(mode, 'template_listens', t('cs.why.tplListens', '{model} is only heard on a bus another master polls: the role is Listen-only.', { model: tplName(tpl) }));
            }
            if (mode === 'poll' && listeners.length) {
                return off(mode, 'role_bus_listened', t('cs.why.busListened', 'Listened to by {who}: another master owns this bus, so the role is Listen-only.', { who: listeners.join(', ') }));
            }
            if (mode === 'listen' && pollers.length) {
                return off(mode, 'role_bus_polled', t('cs.why.busPolled', 'Polled by {who}: a bus is either polled or listened to, never both, so the role is Master.', { who: pollers.join(', ') }));
            }
            if (roleLock) {
                const listenOnly = ['template_listens', 'role_bus_listened'].includes(roleLock.code);
                const master = roleLock.code === 'role_bus_polled';
                if ((listenOnly && mode === 'poll') || (master && mode === 'listen')) return off(mode, roleLock.code, lockText(roleLock, t));
            }
            return ok(mode);
        });
    }

    // the one role a claim, the template or the matrix leaves, and why (null
    // when both fit, or neither: then nothing is pre-filled — the installer
    // answers). The matrix: a combination no device may have (Listen-only on
    // Modbus TCP, L3-UX-12) leaves the other role as the only one there is
    function forcedRole(ctx, roles) {
        const open = roles.filter(r => r.state === 'ok');
        if (open.length !== 1) return null;
        const never = invalidOf(ctx).map(c => c.code);
        const shut = roles.find(r => r.state === 'off'
            && (['template_listens', 'role_bus_listened', 'role_bus_polled'].includes(r.code) || never.includes(r.code)));
        if (!shut) return null;
        return { value: open[0].value, code: shut.code, reason: shut.reason };
    }

    function options(ctx, t = T0) {
        const roles = roleOptions(ctx, t);
        const forced = forcedRole(ctx, roles);
        const ax = ctx.axes || {};
        const tpl = ctx.tpl || null;
        // Seplos V2: never defaulted (M7) — the only role left is said, not chosen
        const prefill = forced && ax.protocol !== 'seplos_v2' && !ctx.role ? forced : null;
        return {
            ports: portOptions(ctx, t), reaches: reachOptions(ctx, t), protocols: protocolOptions(ctx, t),
            roles, prefill, forced, hint: (tpl && tpl.master_hint) || '',
        };
    }

    // a bus change: a Seplos role is asked again (M7); a Modbus one becomes
    // what the new bus's claims leave, or is asked again
    function roleAfterBusChange(ax, role, opts) {
        if ((ax || {}).protocol === 'seplos_v2') return null;
        if (opts && opts.forced) return opts.forced.value;
        return null;
    }

    // what the serial picker judges a port for: an unanswered role is
    // unknown — never a guess (L3-F4)
    function pickerRole(ax, role) {
        return ROLES.includes(role) ? role : 'unknown';
    }

    // ── 2e the line ───────────────────────────────────────────────────────
    function lineInfo(ctx, t = T0) {
        const ax = ctx.axes || {};
        const res = (state, o = {}) => Object.assign({ state, line: null, locked: false, readonly: false, reason: '', fix: null }, o);
        if (ax.port !== 'serial') return res('none');
        const inst = lockOf(ctx, 'connection.line');
        if (inst && inst.code === 'line_installation') return res('installation', { readonly: true, reason: lockText(inst, t), fix: null });
        const bus = ctx.bus || null;
        const self = ctx.selfId || null;
        const others = bus ? (bus.devices || []).filter(d => d.id !== self) : [];
        // an installation's unit or a component on the bus: its line is set
        // there, never by Edit bus line… (m5)
        const owned = others.find(d => d.installation || d.component_of);
        const ownedWhy = owned ? (owned.installation
            ? t('cs.line.byInstallation', 'Set by the installation {inst} ({who} is one of its units): change its connection there.', { inst: owned.installation, who: owned.name || owned.id })
            : t('cs.line.byParent', 'Set by {parent} ({who} is one of its components): change its connection there.', { parent: owned.component_of, who: owned.name || owned.id })) : '';
        if (ax.reach === 'bridge') {
            const b = ctx.bridge || {};
            if (owned) return res('bridge', { readonly: true, fix: 'installation', reason: ownedWhy });
            return b.api
                ? res('bridge', { readonly: true, fix: 'bus', reason: t('cs.line.bridge', 'Set on the bridge {name}: the line is the bridge port’s, for every device on it.', { name: b.name || b.id || '' }) })
                : res('bridge', { readonly: true, fix: 'box', reason: t('cs.line.box', 'Set on the box: {name} keeps its own line settings, the gateway cannot change them.', { name: b.name || b.id || t('cs.line.theConverter', 'the converter') }) });
        }
        if (ax.reach === 'address') {
            return res('converter', { readonly: true, fix: 'box', reason: t('cs.line.converter', 'Set on the box: the converter at this address keeps its own line settings.') });
        }
        if (others.length) {
            // the line of the OTHER devices: the one edited is not its own
            // neighbour, nor a disagreement (m6)
            const groups = [];
            for (const d of others) {
                const key = JSON.stringify(d.line || null);
                const g = groups.find(x => x.key === key);
                if (g) g.who.push(d.name || d.id); else groups.push({ key, line: d.line || null, who: [d.name || d.id] });
            }
            if (owned) return res('installation', { locked: true, line: groups.length === 1 ? fromBus(groups[0].line) : null, fix: 'installation', reason: ownedWhy });
            if (groups.length > 1) {
                const said = groups.map(g => t('cs.line.readsAt', '{who} at {line}', { who: g.who.join(', '), line: lineText(g.line) })).join('; ');
                return res('conflict', { locked: true, fix: 'bus', reason: t('cs.line.conflict', 'The devices on this bus disagree ({said}): a bus has one line. Edit bus line… sets it for all of them.', { said }) });
            }
            const line = fromBus(groups[0].line || bus.line);
            const who = others.map(d => d.name || d.id).join(', ');
            return res('inherited', { locked: true, line, fix: 'bus',
                reason: t('cs.line.inherited', 'Set by the bus: {who} {verb} it at {line}. Edit bus line… changes it for every device on it.',
                    { who, verb: others.length === 1 ? t('cs.line.reads', 'reads') : t('cs.line.read', 'read'), line: lineText(line) }) });
        }
        if (ctx.context === 'edit') return res('device', { reason: t('cs.line.own', 'Alone on its bus: its line is its own.') });
        const tl = ctx.tpl && ctx.tpl.line;
        if (tl) {
            return res('template', { line: Object.assign({}, DEFAULT_LINE, tl),
                reason: t('cs.line.template', 'The line {model} uses by default.', { model: tplName(ctx.tpl) }) });
        }
        return res('default', { line: Object.assign({}, DEFAULT_LINE), reason: t('cs.line.default', '9600 8N1: a common default — check the device.') });
    }

    // ── 2f the address ────────────────────────────────────────────────────
    function addressInfo(ctx, t = T0) {
        const self = ctx.selfId || null;
        const used = ((ctx.bus || {}).units_in_use || []).filter(u => u.device !== self && u.unit != null);
        const taken = new Map(used.map(u => [+u.unit, u.device]));
        const want = Number.isInteger((ctx.tpl || {}).default_unit_id) ? ctx.tpl.default_unit_id : 1;
        let value = want;
        if (taken.has(want)) {
            value = null;
            for (let i = 0; i < 247 && value === null; i++) {
                const u = ((want - 1 + i) % 247) + 1;
                if (!taken.has(u)) value = u;
            }
        }
        const parts = [...taken.entries()].sort((a, b) => a[0] - b[0]).map(([u, d]) => t('cs.addr.is', '{unit} is {who}', { unit: u, who: d }));
        const text = parts.length && value !== null
            ? `${parts.join('; ')}; ${t('cs.addr.free', '{unit} is free', { unit: value })}` : '';
        return { value, used: used.map(u => ({ unit: +u.unit, device: u.device })), text, full: value === null };
    }

    // ── 2g CAN: listen-only is a hard gate ────────────────────────────────
    const canCommand = (name, bitrate) =>
        `ip link set ${name} down && ip link set ${name} type can bitrate ${bitrate || 500000} listen-only on && ip link set ${name} up`;
    function canGate(iface, name, bitrate, t = T0) {
        const n = (iface && iface.name) || name || 'can0';
        const br = (iface && iface.bitrate) || bitrate || 500000;
        const cmd = canCommand(n, br);
        if (!iface) return { ok: false, code: 'not_found', command: cmd, text: t('cs.can.notFound', '{name} is not a CAN interface of this host. Bring it up listen-only first:', { name: n }) };
        if (iface.kind === 'vcan') return { ok: true, code: 'virtual', command: '', text: t('cs.can.virtual', '{name} is virtual: nothing reaches a real bus.', { name: n }) };
        if (iface.listen_only !== true) return { ok: false, code: 'not_listen_only', command: cmd, text: t('cs.can.notListenOnly', '{name} is not listen-only: the gateway reads a CAN bus only through an interface that cannot transmit. On the host:', { name: n }) };
        if (!iface.up) return { ok: false, code: 'down', command: cmd, text: t('cs.can.down', '{name} is down. On the host:', { name: n }) };
        return { ok: true, code: 'ok', command: '', text: t('cs.can.ok', '{name}: up, listen-only, {kbit} kbit/s.', { name: n, kbit: Math.round(br / 1000) }) };
    }

    // ── 2h HTTP: host + device id, the path is the template's ─────────────
    const PLACEHOLDER = /\$\{(?:unit_id|device_id)\}/g;
    function httpUrl(host, id, path) {
        if (!path) return '';
        const h = String(host || '').trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '');
        if (!h) return '';
        return `http://${h}${String(path).replace(PLACEHOLDER, encodeURIComponent(String(id ?? '').trim()))}`;
    }
    function httpParse(url, path) {
        if (!path || !url) return null;
        const m = /^https?:\/\/([^/?#]+)(.*)$/i.exec(String(url).trim());
        if (!m) return null;
        const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const parts = String(path).split(PLACEHOLDER);
        if (parts.length < 2) return m[2] === path ? { host: m[1], id: '' } : null;
        const re = new RegExp('^' + parts.map(esc).join('([^&/#]*)') + '$');
        const got = re.exec(m[2]);
        if (!got) return null;
        const ids = got.slice(1);
        if (ids.some(x => x !== ids[0])) return null;
        return { host: m[1], id: decodeURIComponent(ids[0]) };
    }

    // ── the known bus M is on ─────────────────────────────────────────────
    function busFor(M, buses, serialPorts) {
        M = M || {};
        buses = buses || [];
        const ax = axesOf(M);
        if (ax.port === 'can') return buses.find(b => b.id === `can:${M.channel || 'can0'}`) || null;
        if (ax.reach === 'bridge') {
            return buses.find(b => b.kind === 'bridge' && b.bridge && b.bridge.id === M.bridge && +b.port === +M.bridge_port) || null;
        }
        if (ax.reach === 'here') {
            const sp = String(M.serial_port || '').trim();
            if (!sp) return null;
            const hit = buses.find(b => b.kind === 'serial' && (b.path === sp || b.id === sp));
            if (hit) return hit;
            // another name of a listed port: the bus its claimants are on
            const p = (serialPorts || []).find(x => [x.stable_path, x.path, x.dev, ...(x.by_id || []), ...(x.aliases || [])].includes(sp));
            const who = p ? [...(p.polled_by || []), ...(p.listened_by || [])] : [];
            return who.length ? (buses.find(b => b.kind === 'serial' && (b.devices || []).some(d => who.includes(d.id))) || null) : null;
        }
        if (ax.reach === 'address' && M.host) {
            const id = `${String(M.host).trim().toLowerCase()}:${M.port}`;
            return buses.find(b => b.id === id) || null;
        }
        return null;
    }

    // ── what the step can tell before the server ──────────────────────────
    function errors(ctx, t = T0) {
        const ax = ctx.axes || {};
        const e = {};
        if (ax.port === 'serial' && ax.protocol === 'modbus') {
            const roles = roleOptions(ctx, t);
            const mine = roles.find(r => r.value === ctx.role);
            if (!ctx.role) e['connection.role'] = t('cs.err.role', 'Choose the role on the bus: Master (the gateway polls) or Listen-only (another master polls).');
            else if (mine && mine.state === 'off') e['connection.role'] = mine.reason;
        }
        if (ax.port === 'serial' && ax.protocol === 'seplos_v2' && ctx.role) {
            const mine = roleOptions(ctx, t).find(r => r.value === ctx.role);
            if (mine && mine.state === 'off') e['connection.mode'] = mine.reason;
        }
        if (ax.port === 'can' && ctx.canChecked !== false) {
            const g = canGate(ctx.can || null, ctx.canName, ctx.canBitrate, t);
            if (!g.ok) e['connection.channel'] = `${g.text} ${g.command}`;
        }
        return e;
    }

    const api = { PORTS, REACHES, ROLES, AXES, DEFAULT_LINE, axesOf, applyAxes, options, portOptions, reachOptions,
                  protocolOptions, roleOptions, protocolKey, roleAfterBusChange, pickerRole, lineInfo, lineText,
                  addressInfo, canGate, canCommand, httpUrl, httpParse, busFor, errors };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.ConnStepModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
