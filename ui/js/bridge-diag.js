/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// A bus's health as the serial bridge measures it, next to what the gateway
// sees (multibus/bridge_diag.py, ADR-0017), as one model for the bridge card
// and the Buses / Health dialogs. Pure: no DOM, no fetch, no words — the app
// turns codes into words through its dictionaries. Node tests it directly
// (tools/e2e/bridge_diag_model_test.mjs).
//
//   level(diag)          ok | warn | info | unknown — the colour of the chip
//   code(diag)           the diagnosis code (bridge_diag.CODES), 'none' without one
//   lineRows(stats, r)   [{field, total, perMin, known}] — null counters stay null
//   lineErrors(stats)    total line errors, or null when the adapter counts none
//   activity(stats, now) {client, since, silentS, lastRxAgoS, lastTxAgoS}
//   ago(ts, now)         seconds since ts (null for none)
//   eventTime(e)         an event's time in ms, or null when the bridge sent no number
(function (root) {
    'use strict';

    const CODES = ['no_data', 'unsupported', 'collecting', 'host_overrun', 'wiring', 'device_silent',
                   'network', 'timeouts', 'line_noise', 'ok'];
    const LINE_FIELDS = ['frame', 'parity', 'overrun', 'brk', 'buf_overrun'];

    const code = d => (d && d.diagnosis && CODES.includes(d.diagnosis.code)) ? d.diagnosis.code : 'none';

    function level(d) {
        const c = code(d);
        if (c === 'ok') return 'ok';
        if (c === 'line_noise') return 'info';
        if (c === 'none' || c === 'no_data' || c === 'unsupported' || c === 'collecting') return 'unknown';
        return 'warn';
    }

    const num = v => (typeof v === 'number' && Number.isFinite(v)) ? v : null;

    function lineRows(stats, rates) {
        const line = stats && stats.line;
        return LINE_FIELDS.map(f => {
            const total = line ? num(line[f]) : null;
            const perMin = rates ? num(rates[`${f}_per_min`]) : null;
            return { field: f, total, perMin, known: total !== null };
        });
    }

    function lineErrors(stats) {
        const line = stats && stats.line;
        if (!line) return null;
        const vals = LINE_FIELDS.map(f => num(line[f])).filter(v => v !== null);
        return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
    }

    const ago = (ts, now) => (num(ts) === null ? null : Math.max(0, Math.round(now - ts)));

    function activity(stats, now) {
        const s = stats || {};
        const client = s.client ? (typeof s.client === 'string' ? s.client : s.client.address) : null;
        return {
            client,
            since: s.client && typeof s.client === 'object' ? num(s.client.since) : null,
            silentS: num(s.silent_s),
            lastRxAgoS: ago(s.last_rx_ts, now),
            lastTxAgoS: ago(s.last_tx_ts, now),
        };
    }

    // a bridge's event time, guarded: a bad value must not break the dialog
    const eventTime = e => (e && num(e.ts) !== null ? e.ts * 1000 : null);

    const api = { CODES, LINE_FIELDS, code, level, lineRows, lineErrors, activity, ago, eventTime };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.BridgeDiag = api;
})(typeof window !== 'undefined' ? window : globalThis);
