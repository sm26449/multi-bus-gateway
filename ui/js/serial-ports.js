/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The serial ports of this host (GET /api/serial-ports, ADR-0028) as the
// connection form's port picker sees them. Pure: no DOM, no fetch — Node
// tests it directly (tools/e2e/serial_ports_model_test.mjs).
//
// What the form does on the port decides what it may pick: a poll never goes
// on a bus another device only listens to, a listener never on one another
// device polls (the save refuses both, ADR-0025). The device being edited is
// not its own conflict. A port the container cannot see, the console and a
// port another program holds are shown, never picked, with why.
//
//   role(mode, busMode)  poll | listen | unknown (a Seplos role not answered yet)
//   choice(p, {role, self}) {pickable, why, who, fix}
//     why: '' | listened | polled | console | not_mapped | busy | unknown
//   value(p)             what is stored: a USB adapter's by-id link, else its path
//   matches(p, v)        the stored value names this port
//   label(p), kind(p)
(function (root) {
    'use strict';

    const KINDS = ['usb', 'hat', 'board_uart', 'other'];

    function role(mode, busMode) {
        if (mode === 'rtu_tap') return 'listen';
        // a Seplos V2 bus whose role is not answered yet is judged for
        // neither: no port is refused, none pre-judged (L3-F4)
        if (mode === 'seplos_v2') return busMode === 'listen' ? 'listen' : busMode === 'poll' ? 'poll' : 'unknown';
        return 'poll';
    }

    // what the container has the port as: its by-id link or its own node
    // (stable_path), else a mapped alias (a udev alias node, a renamed
    // mapping — the node /dev/<name> is not in the container then)
    // path_taken: /dev/<name> in this container is another device (a renamed
    // mapping) — never this port's path, nor a value that names it
    const own = p => (p.path_taken ? [] : [p.path, p.dev]);
    const value = p => String(p.stable_path || (p.aliases || [])[0] || own(p).find(Boolean) || '');

    function matches(p, v) {
        const s = String(v || '').trim();
        if (!s) return false;
        return [p.stable_path, ...own(p), ...(p.by_id || []), ...(p.aliases || [])].some(x => x && x === s);
    }

    const others = (list, self) => (list || []).filter(id => id !== self);

    // the port without any device's claim: the server's base_state; an
    // older answer has only state, whose claims are read as free
    function baseState(p) {
        if (p.base_state) return p.base_state;
        return ['listened', 'polled'].includes(p.state) ? 'free' : (p.state || 'free');
    }

    function choice(p, { role: r = 'poll', self = null } = {}) {
        const listened = others(p.listened_by, self), polled = others(p.polled_by, self);
        if (r === 'poll' && listened.length) return { pickable: false, why: 'listened', who: listened, fix: null };
        if (r === 'listen' && polled.length) return { pickable: false, why: 'polled', who: polled, fix: null };
        // what the port is, whoever claims it: a port the container lacks, the
        // console or one another program holds is never picked
        const st = baseState(p);
        if (st === 'console') return { pickable: false, why: 'console', who: [], fix: null };
        if (st === 'not_mapped') return { pickable: false, why: 'not_mapped', who: [], fix: p.fix || null };
        if (st === 'busy') return { pickable: false, why: 'busy', who: p.processes || [], fix: null };
        if (listened.length) return { pickable: true, why: 'listened', who: listened, fix: null };
        if (polled.length) return { pickable: true, why: 'polled', who: polled, fix: null };
        if (st === 'unknown') return { pickable: true, why: 'unknown', who: [], fix: null };
        return { pickable: true, why: '', who: [], fix: null };
    }

    const kind = p => (KINDS.includes(p.kind) ? p.kind : 'other');

    function label(p) {
        const name = p.name || String(p.path || p.dev || '').replace(/^\/dev\//, '');
        const what = p.kind === 'usb' ? (p.model || p.manufacturer || p.label) : (p.label || p.model);
        return what ? `${name} · ${what}` : name;
    }

    const api = { KINDS, role, value, matches, choice, kind, label };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.SerialPorts = api;
})(typeof window !== 'undefined' ? window : globalThis);
