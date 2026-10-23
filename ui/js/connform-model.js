/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// How a device is reached, as one model for every form that says it: Add
// Device (step 1) and the device page's Connection tab. Pure: no DOM, no
// fetch — the forms read and write this, and Node tests it directly.
//
//   model(row)              a GET /api/devices row → M
//   blank(prefill)          a new device's M
//   display(M)              what each field shows, as typed text
//   apply(M, inputs, shown) M with what was typed (only fields that differ
//                           from `shown`; every field when `shown` is null)
//   wire(M)                 the full connection M stands for
//   payload(M, orig, ctx)   what a save sends: Add — the full connection;
//                           Edit — only what was changed (an untouched form
//                           sends {}: the server keeps the connection whole)
//   testConnection(M, orig) what Test sends: the whole connection, saying a
//                           bridge left (never saved)
//   errors(M)               what the form can tell before asking the server
//   pageFields(row), pagePayload(id, d, conn)
//                           the device page's own fields and the PUT it sends
(function (root) {
    'use strict';

    // a serial device path is never a network host (the server refuses it
    // too — FINDINGS BANK-1020: an editor once wrote /dev/ttySEPLOS as a host)
    const SERIAL_PATH = /^(?:\/|\\\\\.\\|COM\d+$|tty[A-Za-z]*\d+$)/i;
    const MODES = ['tcp', 'rtu', 'rtu-tcp', 'rtu_tap', 'http', 'mqtt', 'none'];
    // a mode's class: Modbus variants share a register map; HTTP and MQTT read by path
    const classOf = mode => (mode === 'http' || mode === 'mqtt' || mode === 'none') ? mode : 'modbus';
    const isSerialPath = h => SERIAL_PATH.test(String(h || '').trim());

    // the wire protocol of a stored device → the form's mode and its option
    function modeOf(protocol) {
        const p = String(protocol || 'tcp').toLowerCase();
        if (p === 'udp') return { mode: 'tcp', udp: true, framing: 'rtu' };
        if (p === 'ascii') return { mode: 'rtu', udp: false, framing: 'ascii' };
        if (p === 'ascii-tcp') return { mode: 'rtu-tcp', udp: false, framing: 'ascii' };
        return { mode: MODES.includes(p) ? p : 'tcp', udp: false, framing: 'rtu' };
    }

    function model(row) {
        row = row || {};
        const c = row.connection || {};
        const ser = row.serial || {};
        const { mode, udp, framing } = modeOf(row.protocol || c.protocol);
        const bridged = !!row.bridge;
        return {
            mode, udp, framing,
            // RTU over the network at a typed address (no bridge record): kept
            // as the user's choice, so a redraw never moves it onto a bridge
            legacy: mode === 'rtu-tcp' && !bridged,
            bridge: row.bridge || '', bridge_port: row.bridge_port ?? null,
            host: bridged ? '' : (c.host || row.host || ''),
            port: bridged ? null : (c.port ?? row.port ?? (mode === 'mqtt' ? 1883 : 502)),
            unit_id: c.unit_id ?? row.unit_id ?? 1,
            timeout: c.timeout ?? 3,
            serial_port: c.serial_port || ser.serial_port || '',
            baudrate: c.baudrate || ser.baudrate || 9600,
            parity: c.parity || ser.parity || 'N',
            stopbits: c.stopbits || ser.stopbits || 1,
            bytesize: c.bytesize || ser.bytesize || 8,
            url: c.url || (row.http || {}).url || '',
            broker: c.broker || '',
            mqtt_port: mode === 'mqtt' ? (c.port || 1883) : 1883,
            topic: c.topic || row.mqtt_in_topic || '',
            username: c.username || '',
            password: '',                       // never shown; '' = keep the stored one
            has_password: c.password === '******',
            tls: !!c.tls, tls_insecure: !!c.tls_insecure,
            vars: Object.assign({}, c.vars || {}),
            // the device's own keepalive: an object, false (off), or absent (the template's)
            keepalive: c.keepalive === undefined ? undefined : c.keepalive,
            template_keepalive: c.template_keepalive || null,
        };
    }

    function blank(prefill) {
        const M = model({ protocol: 'tcp', connection: { port: 502, unit_id: 1, timeout: 3 } });
        M.host = '';
        if (prefill) {
            const p = Object.assign({}, prefill);
            if (p.protocol) Object.assign(M, modeOf(p.protocol), p.protocol === 'rtu-tcp' || p.protocol === 'rtu_tap' ? { mode: p.protocol } : {});
            delete p.protocol;
            if ('mqtt_port' in p || 'broker' in p) M.mqtt_port = p.mqtt_port || 1883;
            if ('mqtt_username' in p) { M.username = p.mqtt_username; delete p.mqtt_username; }
            if ('mqtt_password' in p) { M.password = p.mqtt_password; delete p.mqtt_password; }
            if ('mqtt_tls' in p) { M.tls = !!p.mqtt_tls; delete p.mqtt_tls; }
            for (const k of Object.keys(p)) if (k in M) M[k] = p[k];
            // an address given for RTU over the network is the choice made
            if (!('legacy' in p)) M.legacy = M.mode === 'rtu-tcp' && !M.bridge && !!M.host;
        }
        return M;
    }

    // ── what each field shows (text), and reading it back ──────────────────
    // Topic variables as one line of name=value pairs. A comma separates
    // pairs only before the next "name="; a value that itself holds
    // ", name=" writes that comma as \, (shown so, read back as a comma).
    const NEXT_PAIR = /,(?=\s*[A-Za-z_][\w.-]*\s*=)/g;
    const vars2text = v => Object.entries(v || {})
        .map(([k, x]) => `${k}=${String(x).replace(/\\/g, '\\\\').replace(NEXT_PAIR, '\\,')}`).join(', ');
    function text2vars(s) {
        const out = {};
        for (const raw of String(s || '').split(/\n/)) {
            // split on commas that are not escaped and start a new pair
            const parts = [];
            let cur = '';
            for (let i = 0; i < raw.length; i++) {
                const ch = raw[i];
                if (ch === '\\' && i + 1 < raw.length) { cur += ch + raw[i + 1]; i++; continue; }
                if (ch === ',' && /^\s*[A-Za-z_][\w.-]*\s*=/.test(raw.slice(i + 1))) { parts.push(cur); cur = ''; continue; }
                cur += ch;
            }
            parts.push(cur);
            for (const line of parts) {
                const t = line.trim();
                if (!t) continue;
                const i = t.indexOf('=');
                if (i < 1) continue;
                out[t.slice(0, i).trim()] = t.slice(i + 1).trim().replace(/\\(.)/g, '$1');
            }
        }
        return out;
    }
    function kaShown(M) {
        const own = (M.keepalive && typeof M.keepalive === 'object') ? M.keepalive : null;
        const s = own || M.template_keepalive || {};
        return {
            ka_mode: M.keepalive === false ? 'off' : own ? 'custom' : 'template',
            ka_topic: String(s.topic ?? ''),
            ka_first: String(s.first_payload ?? s.payload ?? ''),
            ka_payload: String(s.payload ?? ''),
            ka_every: String(s.every_s ?? 60),
        };
    }

    function display(M) {
        return Object.assign({
            mode: M.mode, udp: !!M.udp, framing: M.framing || 'rtu',
            via: M.bridge ? `${M.bridge}::${M.bridge_port ?? ''}` : '',
            bridge: M.bridge || '', bridge_port: M.bridge_port == null ? '' : String(M.bridge_port),
            host: String(M.host ?? ''), port: M.port == null ? '' : String(M.port),
            unit_id: M.unit_id == null ? '' : String(M.unit_id),
            timeout: String(M.timeout ?? 3),
            serial_port: String(M.serial_port ?? ''),
            baudrate: String(M.baudrate ?? 9600), parity: String(M.parity ?? 'N'),
            stopbits: String(M.stopbits ?? 1), bytesize: String(M.bytesize ?? 8),
            url: String(M.url ?? ''),
            broker: String(M.broker ?? ''), mqtt_port: String(M.mqtt_port ?? 1883),
            topic: String(M.topic ?? ''), username: String(M.username ?? ''), password: '',
            tls: !!M.tls, tls_insecure: !!M.tls_insecure, vars: vars2text(M.vars),
        }, kaShown(M));
    }

    // the bridge M is on, where its mode can be on one at all (direct
    // serial, HTTP and MQTT never are: a bridge left behind by a change of
    // mode does not count)
    const bridgeOf = M => (['tcp', 'rtu-tcp', 'rtu_tap'].includes(M.mode) ? (M.bridge || '') : '');

    const intOrNull = v => (/^-?\d+$/.test(String(v ?? '').trim()) ? parseInt(String(v).trim(), 10) : null);
    const numOrNull = v => { const s = String(v ?? '').trim(); return s === '' || !Number.isFinite(+s) ? null : +s; };

    // M with what was typed. With `shown` (Edit), a field counts only when it
    // differs from what the form showed — an untouched field never turns a
    // stored absence into a value; without it (Add), every field counts.
    function apply(M, inputs, shown) {
        const out = JSON.parse(JSON.stringify(M));
        out.keepalive = M.keepalive;                       // undefined survives the copy
        const touched = k => k in inputs && (!shown || String(inputs[k]) !== String(shown[k]));
        if (touched('mode')) out.mode = inputs.mode;
        if (touched('udp')) out.udp = !!inputs.udp;
        if (touched('framing')) out.framing = inputs.framing === 'ascii' ? 'ascii' : 'rtu';
        if (touched('via')) {
            const [b, p] = String(inputs.via || '').split('::');
            out.bridge = b || '';
            out.bridge_port = b ? intOrNull(p) : null;
        }
        if (touched('bridge')) { out.bridge = inputs.bridge || ''; if (!touched('bridge_port')) out.bridge_port = null; }
        if (touched('bridge_port')) out.bridge_port = intOrNull(inputs.bridge_port);
        for (const k of ['host', 'serial_port', 'url', 'broker', 'topic', 'username', 'parity']) {
            if (touched(k)) out[k] = String(inputs[k]).trim();
        }
        if (touched('password')) out.password = String(inputs.password);
        for (const k of ['port', 'baudrate', 'stopbits', 'bytesize', 'mqtt_port']) if (touched(k)) out[k] = intOrNull(inputs[k]);
        if (touched('unit_id')) out.unit_id = intOrNull(inputs.unit_id);
        if (touched('timeout')) out.timeout = numOrNull(inputs.timeout);
        for (const k of ['tls', 'tls_insecure']) if (touched(k)) out[k] = !!inputs[k];
        if (touched('vars')) out.vars = text2vars(inputs.vars);
        if (['ka_mode', 'ka_topic', 'ka_first', 'ka_payload', 'ka_every'].some(touched)) {
            const mode = inputs.ka_mode ?? (shown || {}).ka_mode ?? 'template';
            const every = numOrNull(inputs.ka_every);
            out.keepalive = mode === 'off' ? false : mode === 'template' ? null : {
                topic: String(inputs.ka_topic ?? '').trim(),
                first_payload: String(inputs.ka_first ?? ''),
                payload: String(inputs.ka_payload ?? ''),
                every_s: every ?? 60,
            };
        }
        // a device off a gateway's bus (or out of RTU onto Modbus TCP) has an
        // address of its own now: the port the form shows is the port it has
        if (out.mode === 'tcp' && !bridgeOf(out) && out.port == null) out.port = 502;
        return out;
    }

    // ── what goes on the wire ──────────────────────────────────────────────
    function wireProto(M) {
        if (M.mode === 'tcp') return M.udp ? 'udp' : 'tcp';
        if (M.mode === 'rtu') return M.framing === 'ascii' ? 'ascii' : 'rtu';
        if (M.mode === 'rtu-tcp') return M.framing === 'ascii' ? 'ascii-tcp' : 'rtu-tcp';
        return M.mode;
    }
    const line = M => ({ baudrate: M.baudrate, parity: M.parity, stopbits: M.stopbits, bytesize: M.bytesize });

    // the whole connection M stands for (Add, Test, a change of how it is reached)
    function wire(M) {
        M = Object.assign({}, M, { bridge: bridgeOf(M) });
        switch (M.mode) {
        case 'none': return { protocol: 'none' };
        case 'http': return { protocol: 'http', url: M.url };
        case 'mqtt': {
            const c = { protocol: 'mqtt', broker: M.broker, port: M.mqtt_port || 1883, topic: M.topic,
                        username: M.username || '', password: M.password || '',
                        tls: !!M.tls, tls_insecure: !!M.tls_insecure, vars: Object.assign({}, M.vars || {}) };
            if (M.keepalive !== undefined) c.keepalive = M.keepalive;
            return c;
        }
        case 'tcp':
            return M.bridge
                ? { bridge: M.bridge, bridge_port: M.bridge_port, unit_id: M.unit_id, timeout: M.timeout }
                : { protocol: wireProto(M), host: M.host, port: M.port, unit_id: M.unit_id, timeout: M.timeout };
        case 'rtu-tcp':
            return M.bridge
                ? { bridge: M.bridge, bridge_port: M.bridge_port, unit_id: M.unit_id, timeout: M.timeout }
                : { protocol: wireProto(M), host: M.host, port: M.port, unit_id: M.unit_id, timeout: M.timeout };
        case 'rtu_tap':
            return M.bridge
                ? { protocol: 'rtu_tap', bridge: M.bridge, bridge_port: M.bridge_port, unit_id: M.unit_id }
                : Object.assign({ protocol: 'rtu_tap', serial_port: M.serial_port }, line(M), { unit_id: M.unit_id });
        default:   // rtu, direct serial (RTU or ASCII framing)
            return Object.assign({ protocol: wireProto(M), serial_port: M.serial_port }, line(M),
                                 { unit_id: M.unit_id, timeout: M.timeout });
        }
    }

    // another wire protocol: the server starts the connection afresh
    // (tuning included). Onto or off a bridge within one protocol is a move:
    // the server keeps the rest (save_merge, same protocol).
    const routeChanged = (M, orig) => wireProto(M) !== wireProto(orig);
    const bridgeMoved = (M, orig) => !!bridgeOf(M) !== !!bridgeOf(orig);
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    // what reaching a bus through a bridge makes stale: the address and the
    // line of its own (a bridge names them), cleared explicitly (null clears)
    const OWN_ROUTE = ['host', 'port', 'serial_port', 'baudrate', 'parity', 'stopbits', 'bytesize'];

    // the connection keys whose value M changes (all of them on a change of
    // how it is reached)
    function changed(M, orig) {
        const now = wire(M), was = wire(orig);
        if (routeChanged(M, orig) || bridgeMoved(M, orig)) return Object.keys(now);
        return Object.keys(now).filter(k => k !== 'protocol' && (k === 'password' ? !!now[k] : !same(now[k], was[k])));
    }

    // what a save sends for the connection. ctx: {context:'add'|'edit', primary}
    function payload(M, orig, ctx) {
        ctx = ctx || {};
        if (ctx.context !== 'edit' || !orig) return wire(M);
        if (ctx.primary) {
            // the primary meter: Modbus TCP, always its address in full (the
            // server answers "unchanged" when it is the one it runs on)
            if (same(wire(M), wire(orig))) return {};
            return { protocol: 'tcp', host: M.host, port: M.port, unit_id: M.unit_id, timeout: M.timeout };
        }
        if (routeChanged(M, orig) || bridgeMoved(M, orig)) {
            const c = wire(M);
            if (bridgeOf(orig) && !bridgeOf(M)) c.bridge = null;    // leaving a bridge is said, never implied
            if (bridgeOf(M) && !bridgeOf(orig)) {
                // onto a bridge: its own address and line go (the bridge names them)
                for (const k of OWN_ROUTE) if (!(k in c)) c[k] = null;
            }
            if (!c.password) delete c.password;              // blank = keep a stored one
            return c;
        }
        const now = wire(M), was = wire(orig);
        const out = {};
        for (const [k, v] of Object.entries(now)) {
            if (k === 'protocol') continue;
            if (k === 'password') { if (v) out.password = v; continue; }
            if (!same(v, was[k])) out[k] = v;
        }
        if (M.mode === 'mqtt' && orig.keepalive !== undefined && M.keepalive === undefined) out.keepalive = null;
        if (!Object.keys(out).length) return {};
        // the protocol rides along so the server judges the same protocol (an
        // address alone would read as a new TCP connection); a device on a
        // bridge has its protocol from the bridge — a tap says it is one
        if (now.protocol && (!bridgeOf(M) || M.mode === 'rtu_tap')) out.protocol = now.protocol;
        return out;
    }

    const dirty = (M, orig) => Object.keys(payload(M, orig, { context: 'edit' })).length > 0;

    // what Test sends (POST /api/devices/test, never saved): the whole
    // connection M holds — the server fills a stored password in — and, as a
    // save would, a bridge left said (the server refuses an address on a
    // bridged device otherwise)
    function testConnection(M, orig) {
        const c = wire(M);
        if (orig && bridgeOf(orig) && !bridgeOf(M)) c.bridge = null;
        return c;
    }

    // ── the device page's own fields (beside the connection) ───────────────
    function pageFields(row) {
        row = row || {};
        const id = row.id || '';
        return {
            name: row.name || id, template: row.template || '', enabled: row.enabled !== false,
            topic_prefix: row.mqtt_topic_prefix || '', bucket: row.influxdb_bucket || '',
            device_tag: row.influxdb_device_tag || id,
            ha_discovery_enabled: row.ha_discovery_enabled !== false,
            mqtt_enabled: row.mqtt_enabled !== false, influxdb_enabled: row.influxdb_enabled !== false,
        };
    }

    // the PUT a device page sends: its name, template, enabled and outputs as
    // shown, and the connection as the form says it — only what was changed
    // ({} keeps it whole: the server's rule). A device that IS its
    // meters sends its identity only (FINDINGS MD-N3).
    function pagePayload(id, d, conn) {
        const none = d.protocol === 'none';
        return {
            id, name: d.name, template: none ? '' : d.template, enabled: d.enabled,
            connection: none ? { protocol: 'none' } : (conn || {}),
            mqtt: Object.assign(d.topic_prefix ? { topic_prefix: d.topic_prefix } : {}, { enabled: d.mqtt_enabled }),
            influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined, enabled: d.influxdb_enabled },
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
    }

    // ── what the form can tell itself ──────────────────────────────────────
    function errors(M, t) {
        t = t || ((k, d) => d);
        const e = {};
        const modbus = classOf(M.mode) === 'modbus';
        if (modbus && (M.unit_id === null || M.unit_id < 0 || M.unit_id > 255)) {
            e['connection.unit_id'] = t('devices.unitIdNeeded', 'Unit ID: a whole number 0–255 (most devices answer on 1).');
        }
        const hostNeeded = (M.mode === 'tcp' || M.mode === 'rtu-tcp') && !M.bridge;
        if (hostNeeded) {
            if (!String(M.host || '').trim()) e['connection.host'] = t('cf.hostNeeded', 'Say the address it is reached at.');
            else if (isSerialPath(M.host)) e['connection.host'] = t('cf.hostIsSerial', 'This is a serial port, not a network address — choose Modbus RTU, direct serial.');
            if (M.port === null || M.port < 1 || M.port > 65535) e['connection.port'] = t('cf.portRange', 'Port: 1–65535.');
        }
        if (modbus && M.mode !== 'rtu_tap' && (M.timeout === null || M.timeout < 1 || M.timeout > 30)) {
            e['connection.timeout'] = t('cf.timeoutRange', 'Timeout: 1–30 seconds.');
        }
        if ((M.mode === 'rtu' || (M.mode === 'rtu_tap' && !M.bridge)) && !String(M.serial_port || '').trim()) {
            e['connection.serial_port'] = t('cf.serialNeeded', 'Say the serial port (for example /dev/ttyUSB0).');
        }
        if ((M.mode === 'tcp' || M.mode === 'rtu-tcp' || M.mode === 'rtu_tap') && M.bridge && M.bridge_port === null && M.mode !== 'tcp') {
            e['connection.bridge_port'] = t('cf.busNeeded', 'Choose the bus.');
        }
        if (M.mode === 'http' && !/^https?:\/\/\S+/i.test(M.url || '')) e['connection.url'] = t('cf.urlNeeded', 'A URL starting with http:// or https://');
        if (M.mode === 'mqtt') {
            if (!String(M.broker || '').trim()) e['connection.broker'] = t('cf.brokerNeeded', 'Say the broker’s address.');
            if (!String(M.topic || '').trim()) e['connection.topic'] = t('cf.topicNeeded', 'Say the topic it publishes on.');
            if (M.mqtt_port === null || M.mqtt_port < 1 || M.mqtt_port > 65535) e['connection.port'] = t('cf.portRange', 'Port: 1–65535.');
            const ka = M.keepalive;
            if (ka && typeof ka === 'object') {
                if (!ka.topic) e['connection.keepalive.topic'] = t('cf.kaTopic', 'Say the keepalive topic.');
                if (!(ka.every_s >= 5 && ka.every_s <= 3600)) e['connection.keepalive.every_s'] = t('cf.kaEvery', 'Every 5–3600 seconds.');
            }
        }
        return e;
    }

    // a server path → the field key the form shows it under, for M's mode
    function fieldOf(path, M) {
        if (path.startsWith('connection.vars')) return 'vars';
        if (path === 'connection.keepalive.topic') return 'ka_topic';
        if (path === 'connection.keepalive.every_s') return 'ka_every';
        if (path.startsWith('connection.keepalive')) return 'ka_mode';
        const k = path.replace(/^connection\./, '');
        if (k === 'port') return M.mode === 'mqtt' ? 'mqtt_port' : 'port';
        if (k === 'bridge' || k === 'bridge_port') return M.mode === 'rtu-tcp' ? k : 'via';
        if (k === 'protocol') return 'mode';
        return ['host', 'unit_id', 'timeout', 'serial_port', 'baudrate', 'parity', 'stopbits', 'bytesize', 'url',
                'broker', 'topic', 'username', 'password', 'tls', 'tls_insecure'].includes(k) ? k : null;
    }

    const api = { MODES, classOf, modeOf, model, blank, display, apply, wire, wireProto, payload, dirty, changed,
                  testConnection, errors, fieldOf, isSerialPath, routeChanged, bridgeMoved, bridgeOf,
                  text2vars, vars2text, pageFields, pagePayload };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.ConnformModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
