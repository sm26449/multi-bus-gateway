/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// A lock in the page's words (L3.10-F16, review 2 N3). The gateway says a
// lock as {field, code, text, params} (multibus/device_summary.py): `text`
// is English. The page says it from its dictionary instead —
//   key(lock)       lock.<code>, the primary's per field (lock.primary.<field>),
//                   a shared line whose devices disagree lock.line_shared_disagree
//   text(lock, t)   the key's words filled with the lock's params; the
//                   server's text (without the "field: " before it) when there
//                   is no key, or when the words need a value the lock does
//                   not carry (an older gateway)
// Every code the server can emit has a key in en and ro
// (tests/test_l311c_lock_text.py reads them from the server's code).
// Also on the page as app._lockText(lock): the Setup's "Fixed, and why" and
// the Edit modal's (app-device-edit.js _deLockText).
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.LockText = api;
    if (typeof JanitzaMonitor !== 'undefined') {
        Object.assign(JanitzaMonitor.prototype, {
            _lockText(lock) { return api.text(lock, (k, d, p) => this.t(k, d, p)); },
        });
    }
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';
    const NONE = '\u0000';

    function serverText(l) {
        const s = String((l && l.text) || '');
        const f = l && l.field;
        return f && s.startsWith(`${f}: `) ? s.slice(f.length + 2) : s;
    }

    function key(l) {
        if (!l || !l.code) return '';
        if (l.code === 'primary') return `lock.primary.${l.field}`;
        if (l.code === 'line_shared' && l.params && l.params.disagree) return 'lock.line_shared_disagree';
        return `lock.${l.code}`;
    }

    function text(l, t) {
        if (!l) return '';
        const k = key(l);
        if (!k || typeof t !== 'function') return serverText(l);
        const params = Object.assign({}, l.params || {});
        const s = t(k, NONE, params);
        // no words for it, or words that need a value this lock does not carry
        if (!s || s === NONE || s === k || /\{\w+\}/.test(s)) return serverText(l);
        return s;
    }

    return { key, text, serverText };
}));
