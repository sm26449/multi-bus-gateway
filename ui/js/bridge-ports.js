/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The serial ports a bridge lists, as one model for every picker that shows
// them: Add Device's adapter list (GET /api/bridge/adapters) and a bridge's
// Buses dialog (GET /api/bridges/<id>/scan). Pure: no DOM, no fetch — Node
// tests it directly (tools/e2e/bridge_ports_model_test.mjs).
//
// A port is offered only when the bridge serves it (status ok, a data port).
// Every other one is still SHOWN — a HAT channel the container may not open,
// a SoC UART not enabled yet, the kernel console, the Bluetooth UART, a port
// another program holds —
// with the reason and, when there is one, the command that fixes it.
//
//   status(a)     ok | busy | no-access | not-enabled | reserved | excluded | unknown
//   selectable(a) the picker may offer it
//   label(a)      what the port is, in words
//   kind(a)       usb | board
//   tcpPort(a)    its data port (adapters: tcp_port; a scan's buses: port)
//   split(list)   {usable: [...], blocked: [...]}, the bridge's order kept
(function (root) {
    'use strict';

    const STATUSES = ['ok', 'busy', 'no-access', 'not-enabled', 'reserved', 'excluded'];

    const tcpPort = a => a.tcp_port ?? a.port ?? null;

    // a bridge before board UARTs sends no status: it listed only what it
    // served, plus the excluded adapters
    function status(a) {
        if (a.status === undefined || a.status === null || a.status === '') {
            if (a.excluded) return 'excluded';
            return a.available !== false && tcpPort(a) ? 'ok' : 'unknown';
        }
        return STATUSES.includes(a.status) ? a.status : 'unknown';
    }

    const selectable = a => status(a) === 'ok' && a.available !== false && !!tcpPort(a);

    const kind = a => (a.kind === 'board' ? 'board' : 'usb');

    function label(a) {
        if (a.label) return String(a.label);
        const name = a.model || a.manufacturer || a.vendor_id || 'serial';
        return a.serial ? `${name} (${a.serial})` : String(name);
    }

    function split(list) {
        const usable = [], blocked = [];
        for (const a of list || []) (selectable(a) ? usable : blocked).push(a);
        return { usable, blocked };
    }

    const api = { STATUSES, status, selectable, kind, label, tcpPort, split };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.BridgePorts = api;
})(typeof window !== 'undefined' ? window : globalThis);
