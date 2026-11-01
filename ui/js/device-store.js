/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The open device and its one write path (ADR-0030).
//
// The device page holds ONE device: its entry and its ETag, as
// GET /api/devices/{id} answered them. Every write the page makes to that
// device goes through devWrite, which
//   - sends `If-Match: <held tag>` (only for the device it holds);
//   - after a 2xx the server CHECKED (`MBG-If-Match: checked`), keeps the
//     tag the answer carries — so the page's own next write is never a
//     false 412;
//   - never takes a tag from a runtime answer (`ignored`): a newer tag there
//     would hide a change made elsewhere;
//   - on a 412 keeps the old tag, does not retry, and tells `onConflict`
//     {current, etag, held, method, path} — the page shows its banner.
// Every other device (a unit's installation, the Types page, a restore) is
// written without If-Match: the store holds nothing for it.
//
//   createStore({fetch})       a store; the browser one is window.DeviceStore
//   store.open(id, entry, etag) / store.clear()
//   store.load(id)             GET /api/devices/{id} → entry (null: failed)
//   store.devWrite(id, method, path, body[, {ifMatch}])
//                              → the fetch Response; writes run one at a
//                              time, in order. A dry run (?dry_run=true)
//                              moves nothing held; ifMatch: the tag sent for
//                              a device the store does not hold
//   store.refresh(id)          re-read the open device after a write of the
//                              page outside /api/devices changed it
//   diff(held, current)        [{path, before, after}] — stored settings only
//
// Pure apart from the fetch it is given: Node tests it
// (tools/e2e/device_store_model_test.mjs).
(function (root) {
    'use strict';

    const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
    // what an entry says about the moment, not about what is stored
    const VOLATILE = new Set(['running', 'meters_status', 'selected_registers', 'energy_fields',
                              'labels', 'kind_source', 'components', 'connection_spec', 'source_specs',
                              'status', 'stats', 'health', 'online', 'age_s', 'can']);

    function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

    // {a: {b: 1}, c: [1, 2]} → {'a.b': 1, c: [1, 2]} (a list is one value)
    function flatten(v, pfx, out) {
        out = out || {};
        if (isObj(v)) {
            const keys = Object.keys(v);
            if (!keys.length && pfx) out[pfx] = {};
            for (const k of keys) {
                if (!pfx && VOLATILE.has(k)) continue;
                flatten(v[k], pfx ? `${pfx}.${k}` : k, out);
            }
        } else if (pfx) {
            out[pfx] = v;
        }
        return out;
    }

    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

    // what differs between the entry the page held and the one stored now,
    // in a stable order; runtime fields left out
    function diff(held, current) {
        const a = flatten(held || {}), b = flatten(current || {});
        const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
        return keys.filter(k => !same(a[k], b[k])).map(k => ({ path: k, before: a[k], after: b[k] }));
    }

    function createStore(opts) {
        const doFetch = (opts && opts.fetch) || ((...a) => root.fetch(...a));
        // ownSaves: the page saved since it read the entry and the answer did
        // not carry the entry — "what changed" may then list its own saves
        const st = { id: null, etag: null, entry: null, conflict: null, ownSaves: false };

        // one request at a time, in order: each write sends the tag the
        // answer before it returned (two quick toggles never send one tag twice)
        let queue = Promise.resolve();
        const enqueue = (job) => {
            const p = queue.then(job, job);
            queue = p.then(() => undefined, () => undefined);
            return p;
        };

        async function write(id, method, path, body, opts) {
            const m = String(method || 'GET').toUpperCase();
            const headers = {};
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            const held = (st.id !== null && st.id === String(id) && st.etag) ? st.etag : null;
            if (held && MUTATING.has(m)) headers['If-Match'] = held;
            // a device the page does not hold, written with the tag the
            // caller read for it (nothing held moves)
            else if (!held && opts && opts.ifMatch && MUTATING.has(m)) headers['If-Match'] = opts.ifMatch;
            const init = { method: m, headers };
            if (body !== undefined) init.body = typeof body === 'string' ? body : JSON.stringify(body);
            const r = await doFetch(path, init);
            if (!held || st.id !== String(id)) return r;
            // a dry run stores nothing: its answer moves nothing held (a
            // 412 is still told — the version is stale before any Save)
            const dry = /[?&]dry_run=true\b/.test(String(path));
            const tag = r.headers.get('ETag');
            if (r.status === 412) {
                let d = {};
                try { d = await r.clone().json(); } catch (e) { /* no body */ }
                const det = (d && d.detail) || {};
                st.conflict = { current: det.current || null, etag: det.etag || tag || null,
                                held: st.entry, ownSaves: st.ownSaves, method: m, path };
                if (typeof store.onConflict === 'function') {
                    try { store.onConflict(st.conflict); } catch (e) { /* the page's own trouble */ }
                }
            } else if (!dry && r.ok && tag && r.headers.get('MBG-If-Match') === 'checked') {
                st.etag = tag;
                // the held entry follows the page's own save when the answer
                // carries it (PUT answers {device: entry})
                let d = null;
                try { d = await r.clone().json(); } catch (e) { /* no body */ }
                const dev = d && d.device;
                if (dev && typeof dev === 'object' && String(dev.id) === st.id) st.entry = dev;
                else st.ownSaves = true;
            }
            return r;
        }

        async function read(id) {
            let r;
            try { r = await doFetch(`/api/devices/${encodeURIComponent(id)}`); } catch (e) { return null; }
            if (!r || !r.ok) return null;
            const d = await r.json().catch(() => null);
            if (!d || !d.device) return null;
            return { entry: d.device, etag: r.headers.get('ETag') };
        }

        const store = {
            onConflict: null,
            get id() { return st.id; },
            get etag() { return st.etag; },
            get entry() { return st.entry; },
            get conflict() { return st.conflict; },
            get ownSaves() { return st.ownSaves; },

            open(id, entry, etag) {
                st.id = id == null ? null : String(id);
                st.entry = entry || null;
                st.etag = etag || null;
                st.conflict = null;
                st.ownSaves = false;
            },

            clear() { store.open(null, null, null); },

            // the device as stored now, with its tag; null when it cannot be read
            async load(id) {
                const got = await read(id);
                if (!got) return null;
                store.open(id, got.entry, got.etag);
                return got.entry;
            },

            // After a write of this page made OUTSIDE /api/devices changed the
            // open device (a unit renamed through PUT /api/endpoints/{id}):
            // those routes carry no tag, so the page takes the new one — in
            // turn with its writes. It also takes a change made elsewhere in
            // that same moment: the cost of an untagged route.
            refresh(id) {
                return enqueue(async () => {
                    if (st.id === null || st.id !== String(id)) return null;
                    const got = await read(id);
                    if (!got || st.id !== String(id)) return null;
                    st.entry = got.entry;
                    st.etag = got.etag || st.etag;
                    st.ownSaves = false;
                    return got.entry;
                });
            },

            // opts.ifMatch: the tag to send for a device the store does not
            // hold (the held device always sends the held tag)
            devWrite(id, method, path, body, opts) {
                return enqueue(() => write(id, method, path, body, opts));
            },
        };
        return store;
    }

    const api = { createStore, diff, flatten, VOLATILE };
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root && root.document) {
        root.DeviceStoreModel = api;
        root.DeviceStore = createStore();
    }
})(typeof window !== 'undefined' ? window : globalThis);
