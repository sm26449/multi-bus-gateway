/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The device page as an operations view (ADR-0031 §3, L3.3): what a device
// IS, said one way, and what may be done to it, built one way.
//
//   kindOf(entry)                 'primary' | 'unit' | 'component' | 'meter-only' | 'device'
//   roleOf(authEnabled, role)     'admin' | 'operator' | 'viewer' (no auth: admin)
//   allows(role, need)            whether a role may use an item that needs `need`
//   summaryView(entry, t, opts)   the technical one-liner and its pills:
//                                 {text, kind, lock, outputs, via} — the device
//                                 page's header, a device list row and a bridge
//                                 card all draw THIS (one render function)
//   deviceActions(entry, role, ctx, t)
//                                 the ⋯ menu: [{group, items: [{id, label, icon,
//                                 need, danger, shortcut, domId, hidden, describedBy}]}]
//                                 — one builder for the page, the list rows and
//                                 the bridge cards (L3-F13); items the role may
//                                 not use are left out, a group left empty goes
//   EDIT_TARGETS / editTarget(id)  where each editing item opens: the Edit
//                                 device modal at a section (every kind,
//                                 ADR-0032 §6), a full-screen editor, or an
//                                 editor of the page — ONE map, the same for
//                                 every kind
//   editKeyFor(entry, view)       the item the E key means where the page is
//   TAB_GROUP / GROUPS / TAB_ALIAS / TAB_SECTION
//                                 the tabs in their four groups; the names of the
//                                 tabs that left (an old link) and the Setup
//                                 section each now opens
//   setupSections(entry, defaults, t)
//                                 the read-only Setup tab: what the Edit modal
//                                 will configure, each value with its default
//
// `t(key, fallback, params)` is the page's translator (keys in
// ui/languages/*.json); a test passes one that fills the fallback.
//
// Pure: Node tests it (tools/e2e/device_view_model_test.mjs).
(function (root) {
    'use strict';

    const RANK = { viewer: 0, operator: 1, admin: 2 };

    function kindOf(e) {
        e = e || {};
        if (e.primary) return 'primary';
        if (e.endpoint_id) return 'unit';
        if (e.component_of) return 'component';
        const p = e.protocol || (e.connection || {}).protocol;
        if (p === 'none') return 'meter-only';
        return 'device';
    }

    function roleOf(authEnabled, role) {
        if (!authEnabled) return 'admin';
        return RANK[role] !== undefined ? role : 'viewer';
    }

    function allows(role, need) {
        return (RANK[role] ?? -1) >= (RANK[need] ?? 99);
    }

    // the page's translator, or (a test, a caller without one) the fallback filled
    const tx = t => (typeof t === 'function' ? t : (k, d, p) => fill(d, p));
    function fill(s, p) {
        return String(s).replace(/\{(\w+)\}/g, (m, k) => (p && p[k] !== undefined ? String(p[k]) : m));
    }

    // a protocol in the words the page uses (the list row's labels)
    const PROTO = { http: 'HTTP', tcp: 'Modbus TCP', udp: 'Modbus UDP', 'rtu-tcp': 'Modbus RTU/TCP',
                    'ascii-tcp': 'Modbus ASCII/TCP', rtu: 'Modbus RTU', ascii: 'Modbus ASCII',
                    rtu_tap: 'Modbus RTU (listen)', seplos_v2: 'Seplos V2', 'seplos_v2-tcp': 'Seplos V2/TCP',
                    mqtt: 'MQTT', can: 'CAN', none: '—' };

    // what the gateway never writes to: a tap, a read-only map (Seplos), CAN
    // (READ_ONLY_PROTOCOLS of multibus/config.py), a device that listens
    const READ_ONLY = ['rtu_tap', 'seplos_v2', 'seplos_v2-tcp', 'can'];
    function listensOnly(e) {
        const spec = e.connection_spec || {};
        const p = e.protocol || (e.connection || {}).protocol;
        if (READ_ONLY.includes(p)) return true;
        if (spec.protocol === 'json_mqtt') return false;             // MQTT listens, and is written by commands
        return spec.mode === 'listen' || (e.summary || {}).role === 'listen-only';
    }

    // whether a device has a Write… at all: a Modbus register map (a tap
    // too — its Write… stays, said listen-only)
    function hasWrite(e) {
        const k = kindOf(e);
        return ['device', 'primary', 'unit'].includes(k) && (e.connection_spec || {}).protocol === 'modbus';
    }

    // what a device may be written through: a Modbus map it polls, or
    // commands — a listen-only bus or a read-only map has nothing to write
    function writable(e) {
        const k = kindOf(e);
        if (k === 'component' || k === 'meter-only') return false;
        if ((e.commands_offered || []).length > 0) return true;
        return (e.connection_spec || {}).protocol === 'modbus' && !listensOnly(e);
    }

    // ── the one-liner ─────────────────────────────────────────────────────
    function summaryText(e, t, opts = {}) {
        t = tx(t);
        const k = kindOf(e);
        if (k === 'unit' && (e.read_via || []).length) {
            const ways = e.read_via.map(r => {
                const iv = r.interval_s != null && r.protocol !== 'rtu_tap' ? ` ${r.interval_s} s` : '';
                return `${r.id} (${PROTO[r.protocol] || r.protocol}${iv})`;
            }).join(' + ');
            return t('ddv.sum.readVia', 'read via {ways}', { ways });
        }
        if (k === 'component') {
            const parent = opts.parent || null;
            const base = parent && parent.summary && parent.summary.text ? parent.summary.text : '';
            const inst = [e.component_type_name || e.component_type, e.component_instance].filter(x => x !== undefined && x !== '').join(' ');
            return [t('ddv.sum.through', 'through {parent}', { parent: (parent && parent.name) || e.component_of }),
                    inst, base].filter(Boolean).join(' · ');
        }
        const s = e.summary;
        if (s && s.protocol === 'json_mqtt') {
            // the broker with its port and TLS; the topic is never in the
            // one-liner (a GX's names its portal) — Setup shows it
            const c = e.connection || {};
            return ['MQTT/JSON', `${s.where || c.broker || '—'}${c.port ? ':' + c.port : ''}${c.tls ? ' · TLS' : ''}`,
                    (s.endpoint_path || c.topic) ? t('ddv.sum.topicHidden', 'topic ••••') : ''].filter(Boolean).join(' · ');
        }
        if (s && s.text) return s.text;
        if (k === 'meter-only') return t('ddv.meterOnlyProto', 'Measured by its meters');
        const p = e.protocol || (e.connection || {}).protocol || '';
        return PROTO[p] || p || '—';
    }

    // the outputs, said with words (never a colour alone): green only when
    // that output publishes now (L3-UX-7) — on here, on for the gateway, and
    // connected; an older gateway that says only on/off is taken at its word
    function outputs(e, t) {
        t = tx(t);
        const s = e.sinks || {};
        const stateOf = (key, flag) => {
            const sk = s[key];
            const on = sk && 'enabled' in sk ? !!sk.enabled : !!flag;
            if (!on) return 'off';
            if (sk && sk.available === false) return 'off-settings';
            if (sk && sk.connected === false) return 'down';
            return 'on';
        };
        const mqtt = stateOf('mqtt', e.mqtt_enabled);
        const rows = [
            ['mqtt', 'MQTT', mqtt, true],
            ['influxdb', 'InfluxDB', stateOf('influxdb', e.influxdb_enabled), true],
            ['ha', 'HA', e.ha_discovery_enabled === false ? 'off' : mqtt, true],
            ['opcua', 'OPC UA', stateOf('opcua', e.opcua_enabled), true],
            ['http', 'HTTP/JSON', stateOf('http', e.http_output_enabled), false],
            ['rest', 'REST', stateOf('rest', e.rest_push_enabled), false],
        ];
        const said = (state, name) => ({
            on: t('ddv.out.on', '{name} on', { name }),
            down: t('ddv.out.down', '{name}: on, not connected', { name }),
            'off-settings': t('ddv.out.offSettings', '{name}: on, but off in Settings', { name }),
            off: t('ddv.out.off', '{name} off', { name }),
        })[state];
        return rows.filter(r => r[3] || r[2] !== 'off').map(([key, label, state]) => ({
            key, label, on: state === 'on', state, said: said(state, label),
        }));
    }

    function via(e, opts = {}) {
        const out = [];
        if (e.bridge) out.push({ kind: 'bridge', id: e.bridge, port: e.bridge_port ?? null });
        if (e.component_of) out.push({ kind: 'parent', id: e.component_of, name: (opts.parent && opts.parent.name) || '' });
        if (e.endpoint_id) out.push({ kind: 'installation', id: e.endpoint_id, name: e.endpoint_name || '' });
        return out;
    }

    function summaryView(e, t, opts = {}) {
        t = tx(t);
        e = e || {};
        const k = kindOf(e);
        // a unit's lock is its installation's (one physical connection): shown,
        // and switched from the unit for every unit of it
        const lock = writable(e)
            ? { locked: e.write_locked === true,
                said: e.write_locked === true ? t('ddv.lock.locked', 'writes locked')
                                              : t('ddv.lock.unlocked', 'writes allowed') }
            : null;
        return { text: summaryText(e, t, opts), kind: k, role: (e.summary || {}).role || null,
                 lock, outputs: k === 'component' ? [] : outputs(e, t), via: via(e, opts) };
    }

    // ── the ⋯ menu (ADR-0032 §1–§3) ───────────────────────────────────────
    // One builder for the device page (every tab), a list row (tree and
    // table), a bridge line and an installation's unit row. The groups are
    // fixed, always in GROUP_ORDER. An item a role may use is always there for
    // its kind: when it cannot run now it is disabled, with one line of why —
    // the facts the server checks — and a `goto` (the item or the place that
    // changes them). An item a role may not use is hidden (§13.4 Q1). A fact
    // the server has not answered yet (what uses it, a template update)
    // leaves its item in place, "checking…": the menu never reflows (§2).
    //
    // ctx: where     'page' | 'list' | 'bridge' | 'installation'
    //      view      {tab, section} on the page: the item E opens says "E"
    //      gateway   {writes, credential} (/api/status `writes`); unknown: not claimed
    //      usedBy    how many things use it (undefined: still asked)
    //      tplUpdate whether its template changed (undefined: still asked)
    //      holdsComponents, can {duplicate, export}, kindLabel(type),
    //      bridgeLineApi(bridgeId) — whether that bridge's line is set from here
    const GROUP_ORDER = ['operate', 'edit', 'data', 'device', 'safety', 'danger', 'restore'];

    // the refusal each reason predicts, as multibus/api.py words it
    // (tests/test_ui_device_actions.py finds every one there): the menu never
    // offers what the server refuses
    const REFUSALS = {
        listen_only: 'devices are read-only — the gateway never',
        locked: 'is write-locked',
        writes_off: 'Writes are disabled',
        no_credential: 'writes require authentication',
        raw_admin: 'unguarded (raw) writes need the admin account',
        primary: 'the primary device cannot be deleted',
        primary_polls: 'the primary device always polls',
        unit: 'is materialized from endpoint',
        component: 'is a component of',
        meter_only: 'nothing to test — measured by',
    };

    // where a reason may send a role (the others are menu items, gated as they are)
    const GOTO_NEED = { security: 'admin', 'security-login': 'admin', usedby: 'viewer' };

    const setOnInstallation = (e, t) => ({ reason: t('ddv.why.setOnInstallation', 'Set on the installation {name}',
        { name: e.endpoint_name || e.endpoint_id }), goto: 'open-installation' });
    const setOnParent = (e, t) => ({ reason: t('ddv.why.setOnParent', 'Set on its device {name}',
        { name: e.parent_name || e.component_of }), goto: 'open-parent' });

    // a write refused now, in the order the server checks: the master
    // switch, a credential to attribute it (write_device_register and
    // _cmd_gate alike), what can never be written (a value only: a command
    // is offered where it can run), then the lock
    function writeBlock(e, role, ctx, t, command) {
        const g = ctx.gateway || {};
        if (g.writes === false) {
            return { code: 'writes_off', reason: t('ddv.why.writesOff', 'Writes are off for the gateway (Settings › Security)'), goto: 'security' };
        }
        if (g.credential === false) {
            return { code: 'no_credential', reason: t('ddv.why.noCredential', 'Writes need a login or an API key (Settings › Security)'), goto: 'security-login' };
        }
        if (!command && listensOnly(e)) {
            return { code: 'listen_only', reason: t('ddv.why.listenOnly', 'Listen-only: the gateway never transmits on this bus') };
        }
        if (e.write_locked === true) {
            return { code: 'locked', goto: 'unlock', reason: allows(role, 'admin')
                ? t('ddv.why.locked', 'Writes locked — Unlock writes… first')
                : t('ddv.why.lockedOther', 'Writes locked — an admin unlocks them') };
        }
        return null;
    }

    const ALL = ['device', 'primary', 'unit', 'component', 'meter-only'];
    const DEV = ['device', 'primary', 'meter-only'];
    const has = (...kinds) => (e, k) => kinds.includes(k);
    const offered = e => (e.commands_offered || []).length > 0;
    // a line lock, in the page's words (the server's text is English): who
    // sets the line instead
    function lineWhy(l, e, t) {
        switch (l.code) {
        case 'line_installation':
        case 'line_bus_installation': return t('ddv.why.lineInstallation', 'Set by the installation {name}: change its connection there', { name: l.installation || '' });
        case 'line_bus_component': return t('ddv.why.lineComponent', 'Set by {name}: one of its components is on this bus', { name: l.parent || '' });
        case 'line_remote': return t('ddv.why.lineRemote', 'Set on the converter, not here');
        case 'line_bridge': return t('ddv.why.lineBridge', 'Set on the bridge {name}: the line is its own', { name: e.bridge || '' });
        default: return String(l.text || '');
        }
    }

    // [id, group, need, present(e, k, ctx), label(t, e, ctx), icon, extra, block(e, k, ctx, t, role)]
    // block: null (it runs), or {reason, code?, goto?, checking?, runs?} —
    // `runs`: still asked, and the action asks the server itself
    const ITEMS = [
        // Operate
        ['test', 'operate', 'operator', has(...ALL), t => t('ddv.test', 'Test'), 'activity', { domId: 'ddvMenuTest' },
            (e, k, ctx, t) => (k === 'component'
                ? { reason: t('ddv.why.testParent', 'Read through its device {name}: test it there', { name: e.parent_name || e.component_of }), goto: 'open-parent' }
                : k === 'meter-only'
                ? { code: 'meter_only', reason: t('ddv.why.testMeters', 'Nothing to test: measured by its meters'), goto: 'meters' }
                : null)],
        ['preview', 'operate', 'operator', has(...ALL), t => t('pv.button', 'Preview'), 'eye', {},
            (e, k, ctx, t) => (k === 'unit'
                ? { reason: t('ddv.why.previewUnit', 'Read through the installation {name}: test its units there', { name: e.endpoint_name || e.endpoint_id }), goto: 'open-installation' }
                : k === 'component' ? setOnParent(e, t)
                : k === 'meter-only' ? { reason: t('ddv.why.readsNothing', 'Measured by its meters: it reads nothing itself'), goto: 'meters' }
                : !e.template ? { reason: t('ddv.why.noTemplate', 'No template: nothing to read with') }
                : null)],
        ['reconnect', 'operate', 'admin', has(...DEV), t => t('devices.reconnect', 'Reconnect'), 'arrow-repeat', {},
            (e, k, ctx, t) => (e.enabled === false ? { reason: t('ddv.why.off', 'It is off: Enable it first'), goto: 'enable' }
                : e.running !== false ? { reason: t('ddv.why.running', 'It is running: nothing to reconnect') }
                : null)],
        ['command', 'operate', 'operator', e => offered(e), t => t('ddv.menu.command', 'Run a command…'), 'send', {},
            (e, k, ctx, t, role) => writeBlock(e, role, ctx, t, true) || ((e.commands || []).length ? null
                : allows(role, 'admin')
                ? { reason: t('ddv.why.noCommand', 'No command is enabled: enable one in Commands & bindings…'), goto: 'commands' }
                : { reason: t('ddv.why.noCommandOther', 'No command is enabled: an admin enables them') })],
        ['write', 'operate', 'operator', e => hasWrite(e), t => t('ddv.menu.write', 'Write a value…'), 'pencil-square', {},
            (e, k, ctx, t, role) => writeBlock(e, role, ctx, t, false)
                || (!allows(role, 'admin') && e.writable_registers === 0
                    ? { code: 'raw_admin', reason: t('ddv.why.rawAdmin', 'Nothing is declared writable; a raw write needs the admin account') }
                    : null)],

        // Edit
        ['edit', 'edit', 'admin', has(...DEV), t => t('ddv.menu.edit', 'Edit device…'), 'pencil', {}, null],
        ['connection', 'edit', 'admin', has(...ALL), t => t('ddv.menu.connection', 'Edit connection…'), 'ethernet', {},
            (e, k, ctx, t) => (k === 'unit' ? setOnInstallation(e, t) : k === 'component' ? setOnParent(e, t)
                : k === 'meter-only' ? { reason: t('ddv.why.noConnection', 'Measured by its meters: it has no connection'), goto: 'meters' }
                : null)],
        ['polling', 'edit', 'admin', has(...ALL), t => t('ddv.menu.polling', 'Polling…'), 'arrow-repeat', {},
            (e, k, ctx, t) => (k === 'unit' ? setOnInstallation(e, t) : k === 'component' ? setOnParent(e, t)
                : k === 'meter-only' ? { reason: t('ddv.why.pollsNothing', 'Measured by its meters: it polls nothing'), goto: 'meters' }
                : null)],
        ['outputs', 'edit', 'admin', has(...ALL), t => t('ddv.menu.outputs', 'Outputs…'), 'signpost-split', {},
            (e, k, ctx, t) => (k === 'component' ? setOnParent(e, t) : null)],
        ['bus-line', 'edit', 'admin', (e, k) => ['device', 'primary'].includes(k) && (e.summary || {}).port === 'serial',
            t => t('cs.line.edit', 'Edit bus line…'), 'sliders', {},
            (e, k, ctx, t) => {
                const locks = (e.locks || []).filter(l => l.field === 'connection.line');
                const owned = locks.find(l => ['line_installation', 'line_bus_installation', 'line_bus_component', 'line_remote'].includes(l.code));
                if (owned) return { reason: lineWhy(owned, e, t) };
                // a bridge keeps its own line unless the row says it is set
                // from here (an older gateway: the page's bridge list says it)
                const br = locks.find(l => l.code === 'line_bridge');
                const fixed = br && (br.settable === false
                    || (br.settable === undefined && typeof ctx.bridgeLineApi === 'function' && !ctx.bridgeLineApi(e.bridge)));
                return fixed ? { reason: lineWhy(br, e, t) } : null;
            }],
        ['rename', 'edit', 'admin', has('unit', 'component'), t => t('ddv.menu.rename', 'Rename…'), 'pencil', {}, null],

        // Data
        ['measurements', 'data', 'admin', has(...ALL), t => t('ddv.menu.measurements', 'Measurements…'), 'list-check', {},
            (e, k, ctx, t) => (k === 'meter-only' ? { reason: t('ddv.why.readsNothing', 'Measured by its meters: it reads nothing itself'), goto: 'meters' } : null)],
        ['download-map', 'data', 'viewer', (e, k) => k !== 'meter-only', t => t('registers.downloadMap', 'Download map'), 'download', {}, null],
        ['calculated', 'data', 'admin', has(...ALL), t => t('ddv.menu.calculated', 'Calculated…'), 'calculator', {}, null],
        ['commands', 'data', 'admin', e => offered(e), t => t('ddv.menu.commands', 'Commands & bindings…'), 'send', {},
            (e, k, ctx, t) => (k === 'unit' ? { reason: t('ddv.why.commandsInstallation', 'Enabled in its installation\'s group'), goto: 'open-installation' } : null)],
        ['meters', 'data', 'admin', has(...DEV), t => t('ddv.menu.meters', 'Meters…'), 'lightning', {}, null],
        ['components', 'data', 'admin', (e, k, ctx) => ['device', 'primary'].includes(k) && (!!ctx.holdsComponents || holdsComponents(e, null)),
            t => t('ddv.menu.components', 'Components…'), 'boxes', {}, null],
        ['template-update', 'data', 'admin', (e, k) => ['device', 'meter-only'].includes(k) && !!e.template,
            t => t('ddv.menu.templateUpdate', 'Update from template…'), 'stars', {},
            (e, k, ctx, t) => (ctx.tplUpdate === undefined ? { checking: true, reason: t('ddv.why.checking', 'checking…') }
                : !ctx.tplUpdate ? { reason: t('ddv.why.tplSame', 'Its template has not changed since') } : null)],

        // Device
        ['type', 'device', 'admin', has('device', 'meter-only'),
            // what it is now, in the page's words (ctx.kindLabel) or the gateway's
            (t, e, ctx) => t('ddv.menu.typeOf', 'Type: {type}…', { type: ctx.kindLabel ? ctx.kindLabel(e.type || e.kind) : (e.type_label || e.type || e.kind || '—') }),
            'tag', { domId: 'ddvSetKind' }, null],
        ['about', 'device', 'admin', has(...ALL), t => t('ddv.menu.about', 'About (attributes)…'), 'tags', {}, null],
        ['duplicate', 'device', 'admin', (e, k, ctx) => k === 'device' && !!(ctx.can || {}).duplicate,
            t => t('ddv.menu.duplicate', 'Duplicate…'), 'copy', {}, null],
        ['export', 'device', 'viewer', (e, k, ctx) => !!(ctx.can || {}).export, t => t('ddv.menu.export', 'Export…'), 'download', {}, null],
        ['open-installation', 'device', 'viewer', has('unit'), t => t('devices.openInstallation', 'Open installation'), 'diagram-3', {}, null],
        ['open-parent', 'device', 'viewer', has('component'), t => t('components.openParent', 'Open device'), 'cpu', {}, null],
        ['setup', 'device', 'viewer', has(...ALL), t => t('ddv.menu.setup', 'Show settings'), 'sliders', {}, null],

        // Safety
        ['lock', 'safety', 'admin', has('device', 'primary', 'unit'),
            (t, e) => (e.write_locked === true ? t('ddv.menu.unlock', 'Unlock writes…') : t('ddv.menu.lock', 'Lock writes')),
            'lock', { domId: 'ddvMenuLock' },
            (e, k, ctx, t) => (writable(e) ? null
                : listensOnly(e) ? { reason: t('ddv.why.nothingListen', 'Listen-only: nothing is ever written to it') }
                : { reason: t('ddv.why.nothingToWrite', 'Nothing to write: the gateway never writes to it') })],
        ['disable', 'safety', 'admin', has(...ALL),
            (t, e) => (e.enabled === false ? t('ddv.enable', 'Enable') : t('ddv.menu.disable', 'Disable…')),
            'pause-circle', { domId: 'ddvEnableToggle' },
            (e, k, ctx, t) => (k === 'unit' ? setOnInstallation(e, t)
                // the server's lock (device_patch.PRIMARY_FIXED): it is never switched off
                : k === 'primary' ? { code: 'primary_polls', reason: t('ddv.why.primaryPolls', 'The primary meter always polls: it is the gateway\'s own meter') }
                // a component of a device that is off is off whatever its own switch says
                : k === 'component' && e.parent_enabled === false
                ? { reason: t('ddv.why.parentOff', 'Its device {name} is off: enable it there', { name: e.parent_name || e.component_of }), goto: 'open-parent' }
                : null)],

        // Danger
        ['delete', 'danger', 'admin', has(...ALL),
            (t, e) => (kindOf(e) === 'component' ? t('ddv.menu.remove', 'Remove…') : t('ddv.menu.delete', 'Delete…')),
            'trash', { danger: true, domId: 'ddvDeleteBtn' },
            (e, k, ctx, t) => {
                if (k === 'primary') return { code: 'primary', reason: t('ddv.why.primaryDelete', 'The primary device cannot be deleted') };
                if (k === 'unit') return { code: 'unit', ...setOnInstallation(e, t) };
                if (k === 'component') return null;
                if (ctx.usedBy === undefined) return { checking: true, runs: true, reason: t('ddv.why.checkingUse', 'checking what uses it…') };
                // the gateway could not be asked: never said free, never held —
                // Delete… asks it again
                if (ctx.usedBy === null) return { runs: true, reason: t('ddv.why.usedUnknown', 'what uses it is not known: Delete… asks the gateway') };
                return ctx.usedBy > 0 ? { reason: t('ddv.why.usedBy', 'Used by {n} — see Used by', { n: ctx.usedBy }), goto: 'usedby' } : null;
            }],

        // the closing line
        ['restore', 'restore', 'admin', () => true, t => t('ddv.menu.restore', 'Deleted devices: restore…'), 'arrow-counterclockwise', {}, null],
    ];
    // an item named by its state (the same place in the menu either way)
    const ID_OF_STATE = { lock: e => (e.write_locked === true ? 'unlock' : 'lock'),
                          disable: e => (e.enabled === false ? 'enable' : 'disable') };
    const ICON_OF_STATE = { unlock: 'unlock', enable: 'play-circle' };

    function deviceActions(e, role, ctx, t) {
        t = tx(t);
        e = e || {};
        ctx = Object.assign({ where: 'page' }, ctx || {});
        const k = kindOf(e);
        const page = ctx.where === 'page';
        const groups = {};
        const all = [];
        for (const [base, group, need, present, label, icon, extra, block] of ITEMS) {
            if (!allows(role, need) || !present(e, k, ctx)) continue;
            const id = ID_OF_STATE[base] ? ID_OF_STATE[base](e) : base;
            const b = (block && block(e, k, ctx, t, role)) || null;
            // the page's own ids (one page at a time); a list holds many rows
            const item = { id, group, need, label: label(t, e, ctx), icon: ICON_OF_STATE[id] || icon, danger: !!extra.danger,
                           shortcut: '', disabled: !!b && !b.runs, reason: b ? b.reason : '', code: (b && b.code) || '',
                           goto: (b && b.goto) || '', checking: !!(b && b.checking),
                           domId: page ? (extra.domId || '') : '', describedBy: page ? (extra.describedBy || '') : '' };
            (groups[group] = groups[group] || []).push(item);
            all.push(item);
        }
        // a reason's way to somewhere the role may not go is left out
        for (const it of all) {
            if (!it.goto) continue;
            const target = all.find(x => x.id === it.goto);
            if (!(target ? !target.disabled : allows(role, GOTO_NEED[it.goto] || 'admin'))) it.goto = '';
        }
        // E (the page, an admin): the item it opens says so
        if (page && ctx.view && allows(role, 'admin')) {
            const key = editKeyFor(e, ctx.view);
            const target = all.find(x => x.id === key)
                || (MODAL_SECTIONS.includes(key) ? all.find(x => x.id === 'edit') : null);
            if (target) target.shortcut = 'E';
        }
        return GROUP_ORDER.filter(g => groups[g]).map(g => ({ group: g, items: groups[g] }));
    }

    // the header's operate buttons: the menu's own items (§5) — Test and
    // Preview always, Write… and Run a command… where the kind has them
    const HEADER = ['test', 'preview', 'command', 'write'];
    function headerActions(e, role, ctx, t) {
        const items = deviceActions(e, role, ctx, t).flatMap(g => g.items);
        return HEADER.map(id => items.find(x => x.id === id)).filter(Boolean);
    }

    // ── where an editing item opens (ADR-0032 §6: one way to edit) ─────────
    // modal: the Edit device modal, at that section (focus: a field in it) —
    //        every kind: the primary (its PATCH adapter, L3.11a), a unit and
    //        a component (their own fields; the rest said where it is set)
    // full:  a large editor, full screen with the edit bar and one Save (part:
    //        where it opens) — the same editor from the page, a list or ⋯
    // panel: an editor that is no longer a tab, shown in place
    // tab:   that tab's panel (Components: the parent's Overview card)
    const EDIT_TARGETS = {
        connection: { modal: 'connection' },
        polling: { modal: 'polling' },
        identity: { modal: 'identity' },
        type: { modal: 'identity', focus: 'type' },
        outputs: { modal: 'outputs' },
        rename: { modal: 'identity', focus: 'name' },
        measurements: { full: 'measurements', part: 'selected' },
        calculated: { full: 'measurements', part: 'calculated' },
        commands: { full: 'commands' },
        meters: { panel: 'meters', section: 'meters' },
        about: { panel: 'attributes', section: 'identity' },
        components: { tab: 'overview', focus: '#devComponentsCard' },
    };
    // the Edit device modal's sections (where Edit device… opens)
    const MODAL_SECTIONS = ['connection', 'polling', 'identity', 'outputs'];

    // where item `id` opens — the same for every kind (`kind` kept for callers)
    function editTarget(id, kind) {    // eslint-disable-line no-unused-vars
        const tg = EDIT_TARGETS[id];
        return tg ? Object.assign({}, tg) : null;
    }

    // E: the editor of what is in view (§7) — a data tab its own, a Setup
    // section its own, else the connection (a device measured by its meters
    // has none: its identity). A unit or a component: its data tab's
    // editor, a unit's outputs (its installation's), else its name.
    function editKeyFor(e, view = {}) {
        const k = kindOf(e);
        const tab = view.tab || '';
        const section = tab === 'setup' ? view.section || '' : '';
        if (k === 'unit' || k === 'component') {
            if (tab === 'measurements' || tab === 'calculated') return tab;
            if (k === 'unit' && section === 'outputs') return 'outputs';
            return 'rename';
        }
        if (tab === 'measurements' && k !== 'meter-only') return 'measurements';
        if (tab === 'calculated') return 'calculated';
        if (tab === 'commands' && offered(e)) return 'commands';
        const bySection = { outputs: 'outputs', pq: 'outputs', meters: 'meters', identity: 'identity', polling: 'polling' };
        if (bySection[section]) return bySection[section];
        if (section === 'writes' && offered(e)) return 'commands';
        if (section === 'components' && (e.components || 0) > 0) return 'components';
        return k === 'meter-only' ? 'identity' : 'connection';
    }

    // Edit device…: the modal at the section in view, else at its first
    // (Connection; Identity for a device measured by its meters)
    function editSectionFor(e, view = {}) {
        const key = editKeyFor(e, view);
        if (MODAL_SECTIONS.includes(key)) return key;
        return kindOf(e) === 'meter-only' ? 'identity' : 'connection';
    }

    // ── tabs ──────────────────────────────────────────────────────────────
    const GROUPS = ['live', 'data', 'setup', 'diagnose'];
    const TAB_GROUP = { overview: 'live', history: 'live', energy: 'live', pq: 'live',
                        measurements: 'data', calculated: 'data', commands: 'data',
                        setup: 'setup', monitor: 'diagnose', logs: 'diagnose' };
    // earlier names of a tab, and the tabs that left: an old link opens where
    // their content is now
    const TAB_ALIAS = { registers: 'measurements', regs: 'measurements', values: 'overview',
                        edit: 'setup', connection: 'setup', outputs: 'setup', attributes: 'setup',
                        meters: 'setup', configure: 'setup', polling: 'setup', identity: 'setup' };
    const TAB_SECTION = { edit: 'connection', connection: 'connection', outputs: 'outputs',
                          attributes: 'identity', meters: 'meters', polling: 'polling', identity: 'identity' };

    // ── Setup (read-only) ─────────────────────────────────────────────────
    const isSet = v => v !== undefined && v !== null && v !== '';

    // a URL as Setup says it: its userinfo and every query value masked (a
    // password, a token), for every role — what it reaches stays readable
    function maskUrl(u) {
        if (!isSet(u)) return '';
        const s = String(u);
        const m = /^([a-z][a-z0-9+.-]*:\/\/)([^/?#@]*@)?([^?#]*)(\?[^#]*)?(#.*)?$/i.exec(s);
        if (!m) return s;
        const q = m[4] ? '?' + m[4].slice(1).split('&').map(p => {
            const i = p.indexOf('=');
            return i < 0 ? p : p.slice(0, i + 1) + '••••';
        }).join('&') : '';
        return `${m[1]}${m[2] ? '••••@' : ''}${m[3]}${q}${m[5] || ''}`;
    }

    // one rule for "it has components", on the page and in a list: its
    // template declares them (the row says so: components_declared), or it
    // holds some
    function holdsComponents(e, tpl) {
        e = e || {};
        return (e.components || 0) > 0 || e.components_declared === true || ((tpl || {}).components || []).length > 0;
    }

    // a value with where it comes from: "3 s (default)" when it is the default
    function valued(v, dflt, unit, t) {
        t = tx(t);
        const shown = isSet(v) ? `${v}${unit ? ' ' + unit : ''}` : (isSet(dflt) ? `${dflt}${unit ? ' ' + unit : ''}` : '—');
        const isDefault = !isSet(v) || (isSet(dflt) && String(v) === String(dflt));
        return { value: shown, note: isDefault && isSet(dflt) ? t('ddv.setup.default', 'default') : '' };
    }

    function setupSections(e, defaults, t) {
        t = tx(t);
        e = e || {};
        const k = kindOf(e);
        const s = e.summary || {};
        const c = e.connection || {};
        const md = ((defaults || {}).modbus) || {};
        const dv = key => (md[key] || {}).value;
        const row = (id, label, value, note = '') => ({ id, label, value: isSet(value) ? String(value) : '—', note });
        const out = [];

        // Connection
        const conn = [];
        if (k === 'unit') {
            conn.push(row('summary', t('ddv.setup.how', 'How it is read'), summaryText(e, t)));
            conn.push(row('owner', t('ddv.setup.setOn', 'Set on'),
                          t('ddv.setup.installation', 'the installation {name}', { name: e.endpoint_name || e.endpoint_id })));
        } else if (k === 'component') {
            conn.push(row('summary', t('ddv.setup.how', 'How it is read'), summaryText(e, t)));
            conn.push(row('owner', t('ddv.setup.setOn', 'Set on'),
                          t('ddv.setup.parent', 'its device {name}', { name: e.component_of })));
        } else if (k === 'meter-only') {
            conn.push(row('summary', t('ddv.setup.how', 'How it is read'), summaryText(e, t)));
            conn.push(row('measured', t('ddv.setup.measuredBy', 'Measured by'), (e.measured_by || []).join(', ')));
        } else {
            conn.push(row('summary', t('ddv.setup.line', 'Connection'), summaryText(e, t)));
            if (s.role) conn.push(row('role', t('ddv.setup.role', 'Role on the bus'),
                                      s.role === 'listen-only' ? 'Listen-only' : 'Master'));
            if (s.bridge) conn.push(row('bridge', t('ddv.setup.bridge', 'Bridge'),
                                        `${s.bridge.id} · port ${s.bridge.port ?? '—'}${s.bridge.host ? ` (${s.bridge.host})` : ''}`));
            else if (s.protocol === 'json_mqtt') {
                // the broker as the line says it, with its port and TLS (the one-liner names the broker only)
                conn.push(row('where', t('ddv.setup.broker', 'Broker'),
                              `${s.where || c.broker || '—'}${c.port ? ':' + c.port : ''}${c.tls ? ' · TLS' : ''}`));
                // the topic can carry a GX's portal id: never said here (the
                // connection editor shows it behind its eye)
                if (isSet(c.topic)) conn.push(row('topic', t('ddv.setup.topic', 'Topic'), '••••'));
            } else if (s.protocol === 'json_http' && isSet(c.url)) conn.push(row('where', 'URL', maskUrl(c.url)));
            else if (isSet(s.where)) conn.push(row('where', t('ddv.setup.where', 'Address'), s.where));
            if (s.line) conn.push(row('line', t('ddv.setup.serialLine', 'Line'),
                                      `${s.line.baud} ${s.line.bytesize}${s.line.parity}${s.line.stopbits}`));
            if (isSet(s.unit)) conn.push(row('unit', s.address_kind === 'pack' ? t('ddv.setup.pack', 'Pack') : t('ddv.setup.unit', 'Unit ID'), s.unit));
            if (s.protocol === 'modbus' || s.protocol === 'seplos_v2') {
                const to = valued(c.timeout, dv('timeout'), 's', t);
                conn.push(row('timeout', t('ddv.setup.timeout', 'Timeout'), to.value, to.note));
                const ra = valued(c.retry_attempts, dv('retry_attempts'), '', t);
                conn.push(row('retries', t('ddv.setup.retries', 'Retries'), ra.value, ra.note));
                const rd = valued(c.retry_delay, dv('retry_delay'), 's', t);
                conn.push(row('retry_delay', t('ddv.setup.retryDelay', 'Retry delay'), rd.value, rd.note));
            }
        }
        out.push({ id: 'connection', title: t('ddv.setup.connection', 'Connection'), rows: conn,
                   // the connection's own locks (a primary's identity, polling and
                   // outputs locks are not its connection's)
                   locks: (e.locks || []).filter(l => String(l.field || '').startsWith('connection.'))
                       .map(l => ({ field: l.field, code: l.code, text: l.text })) });

        // Polling
        const poll = [row('enabled', t('ddv.setup.polling', 'Acquisition'),
                          e.enabled === false ? t('ddv.setup.off', 'off') : t('ddv.setup.on', 'on'))];
        if (s.poll_s) poll.push(row('poll_s', t('ddv.setup.pollEvery', 'Read every'),
                                    s.poll_s[0] === s.poll_s[1] ? `${s.poll_s[0]} s` : `${s.poll_s[0]}–${s.poll_s[1]} s`));
        else if (s.role === 'listen-only') poll.push(row('poll_s', t('ddv.setup.pollEvery', 'Read every'),
                                                         t('ddv.setup.listened', 'never asked: it is heard as its master asks')));
        out.push({ id: 'polling', title: t('ddv.setup.pollingTitle', 'Polling'), rows: poll });

        // Identity
        const ident = [row('name', t('devices.wizard.name', 'Device name'), e.name || e.id),
                       row('id', 'ID', e.id),
                       row('type', t('find.facet.kind', 'Type'), e.type_label || e.type || e.kind),
                       row('template', t('devices.wizard.template', 'Template'), e.template || (k === 'meter-only' ? '' : t('ddv.setup.noTemplate', 'none')))];
        out.push({ id: 'identity', title: t('ddv.setup.identity', 'Identity'), rows: ident });

        // Outputs
        if (k !== 'component') {
            const pub = e.publishing || {};
            const mode = svc => {
                const own = ((pub.own || {})[svc] || {}).publish_mode;
                const eff = ((pub.effective || {})[svc] || {}).publish_mode;
                return isSet(eff) ? `${eff}${isSet(own) ? '' : ' (' + t('ddv.setup.gatewayDefault', 'gateway default') + ')'}` : '';
            };
            const sk = e.sinks || {};
            const onOff = v => (v ? t('ddv.setup.on', 'on') : t('ddv.setup.off', 'off'));
            const outs = [
                row('mqtt', 'MQTT', [onOff(sk.mqtt ? sk.mqtt.enabled : e.mqtt_enabled), (sk.mqtt || {}).topic_prefix || e.mqtt_topic_prefix, mode('mqtt')].filter(isSet).join(' · ')),
                row('influxdb', 'InfluxDB', [onOff(sk.influxdb ? sk.influxdb.enabled : e.influxdb_enabled), (sk.influxdb || {}).bucket || e.influxdb_bucket, mode('influxdb')].filter(isSet).join(' · ')),
                row('ha', t('ddv.setup.ha', 'Home Assistant discovery'), onOff(e.ha_discovery_enabled !== false)),
                row('opcua', 'OPC UA', [onOff(sk.opcua ? sk.opcua.enabled : e.opcua_enabled),
                    (e.opcua_fields || []).length ? t('devices.sink.opcuaCount', '{n} chosen', { n: e.opcua_fields.length })
                                                 : t('devices.sink.opcuaAll', 'Every value')].join(' · ')),
                row('http', t('devices.sink.httpTitle', 'HTTP / JSON output'), [onOff(sk.http ? sk.http.enabled : e.http_output_enabled), (sk.http || {}).path].filter(isSet).join(' · ')),
                row('rest', t('rest.title', 'REST push'), [onOff(sk.rest ? sk.rest.enabled : e.rest_push_enabled), maskUrl((sk.rest || {}).url)].filter(isSet).join(' · ')),
            ];
            out.push({ id: 'outputs', title: t('devices.detail.outputs', 'Outputs'), rows: outs,
                       note: k === 'unit' ? t('devices.sink.endpointLevel', 'Set on the installation — applies to every unit') : '' });
        }

        // the PQ event recorder (a meter that records it): its settings, defaults marked
        if (e.pq_supported && k !== 'unit') {
            const pr = e.pq_recorder || {};
            const host = c.host || e.host || '';
            const onOff = v => (v ? t('ddv.setup.on', 'on') : t('ddv.setup.off', 'off'));
            const poll = valued(pr.poll_s, 300, 's', t);
            const base = valued(isSet(pr.base_url) ? maskUrl(pr.base_url) : '', host ? `http://${host}` : '', '', t);
            out.push({ id: 'pq', title: t('pq.sinkTitle', 'PQ event recorder'), rows: [
                row('enabled', t('ddv.setup.pqOn', 'Recording'), onOff(!!pr.enabled)),
                row('poll_s', t('pq.pollS', 'Poll (s)'), poll.value, poll.note),
                row('base_url', t('pq.baseUrl', 'Base URL'), base.value, base.note),
                row('archive', t('ddv.setup.pqArchive', 'Waveforms archived'), onOff(pr.archive_waveforms !== false),
                    pr.archive_waveforms === undefined ? t('ddv.setup.default', 'default') : ''),
            ] });
        }

        // Write protection
        if (writable(e)) {
            const w = [row('lock', t('devices.writeLock.title', 'Write protection'),
                           e.write_locked === true ? t('devices.writeLock.locked', 'locked') : t('devices.writeLock.unlocked', 'writes allowed'))];
            if ((e.commands_offered || []).length) {
                w.push(row('commands', t('commands.tab', 'Commands'),
                           t('ddv.setup.commandsOn', '{n} of {m} enabled', { n: (e.commands || []).length, m: e.commands_offered.length })));
            }
            out.push({ id: 'writes', title: t('ddv.setup.writes', 'Writes'), rows: w });
        }

        // Meters
        if (DEV.includes(k)) {
            const links = (e.meters || []).map(m => row(`meter-${m.role}`, m.role, m.device));
            out.push({ id: 'meters', title: k === 'meter-only' ? t('ddv.setup.measuredByTitle', 'Measured by') : t('meters.tab', 'Meters'),
                       rows: links.length ? links : [row('none', t('meters.tab', 'Meters'), t('ddv.setup.noMeters', 'none'))] });
        }

        // Components
        if (e.components) {
            out.push({ id: 'components', title: t('ddv.setup.components', 'Components'),
                       rows: [row('count', t('ddv.setup.componentsCount', 'Held'), e.components)] });
        }
        return out;
    }

    const api = { kindOf, roleOf, allows, writable, listensOnly, hasWrite, maskUrl, holdsComponents, summaryText, summaryView,
                  deviceActions, headerActions, GROUP_ORDER, REFUSALS, EDIT_TARGETS, editTarget, editKeyFor, editSectionFor,
                  GROUPS, TAB_GROUP, TAB_ALIAS, TAB_SECTION, setupSections, PROTO };
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root && root.document) root.DeviceViewModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
