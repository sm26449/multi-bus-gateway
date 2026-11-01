/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// A command's binding (auto-revert, safe value) and its channel's pace, as
// one model for the two forms that edit them: a device's Commands tab and an
// installation group's editor (ADR-0019). Pure: no DOM, no fetch — the forms
// read and write this, and Node tests it directly.
//
//   channels(cmds)              the commands grouped by channel, in the
//                               order they are listed: [{cid, key, label, cmds, tpl, override}]
//   channelFields(ch)           the inputs a channel shows: [{k, adv, tpl}]
//   channelShown(ch)            {k: text} what each input starts with
//   readChannel(ch, typed)      {payload (changed keys only, null removes),
//                                errors {k: {code, p}}, below (typed interval
//                                under the template's), dirty}
//   editable(cmd)               whether a command has a binding to edit
//   safeKeys(cmd)               the parameters its safe value names
//   bindingShown(cmd)           {lease_s: text, safe: {k: text}}
//   readBinding(cmd, typed)     {payload (changed keys only), errors, dirty,
//                                safe (the effective one), note}
//   groupCommands(cmds, state)  an installation group's bindings to store
//   groupChannels(stored, chs, typed)  its channel overrides to store
//
// Error codes (the page words them): number, whole, range, wholeRange,
// maxAge, below, above, allowed, leaseNoSafe.
(function (root) {
    'use strict';

    const MAX_S = 86400;
    const DEFAULT_MAX_AGE_S = 30;
    // what a device may set on a channel; `combine` stays the template's
    const ADVANCED = ['min_on_s', 'min_off_s', 'max_starts_per_h'];
    const has = v => v !== undefined && v !== null && v !== '';
    const text = v => (has(v) ? String(v) : '');
    const isEmpty = o => !o || !Object.keys(o).length;

    // a channel per listed command, in the order the template lists them
    function channels(cmds) {
        const out = [];
        const byCid = new Map();
        for (const c of cmds || []) {
            const cid = c.channel || c.name;
            let ch = byCid.get(cid);
            if (!ch) {
                const tpl = c.template_channel || {};
                ch = { cid, key: c.channel_key || cid, label: tpl.label || '', cmds: [],
                       tpl, override: Object.assign({}, c.channel_override || {}),
                       effective: Object.assign({}, c.channel_settings || {}) };
                byCid.set(cid, ch);
                out.push(ch);
            }
            ch.cmds.push(c);
        }
        return out;
    }

    // min interval always; the rest under Advanced — max age always there,
    // the motor's protections only when the template's channel has them
    function channelFields(ch) {
        const tpl = (ch && ch.tpl) || {};
        const out = [{ k: 'min_interval_s', adv: false, tpl: Number(tpl.min_interval_s) || 0 }];
        out.push({ k: 'max_age_s', adv: true, tpl: has(tpl.max_age_s) ? Number(tpl.max_age_s) : DEFAULT_MAX_AGE_S });
        for (const k of ADVANCED) if (has(tpl[k])) out.push({ k, adv: true, tpl: Number(tpl[k]) });
        return out;
    }

    function channelShown(ch) {
        const o = (ch && ch.override) || {};
        const out = {};
        for (const f of channelFields(ch)) out[f.k] = text(o[f.k]);
        return out;
    }

    // a seconds field: '' is "the template's"; otherwise 0..86400
    function numErr(k, raw) {
        const v = Number(raw);
        if (!/^-?\d+(\.\d+)?$/.test(raw) || !Number.isFinite(v)) return { code: 'number' };
        if (v < 0 || v > MAX_S) return { code: 'range', p: { min: 0, max: MAX_S } };
        if (k === 'max_starts_per_h' && !Number.isInteger(v)) return { code: 'whole' };
        if (k === 'max_age_s' && v < 1) return { code: 'maxAge', p: { min: 1, max: MAX_S } };
        return null;
    }

    function readChannel(ch, typed) {
        const shown = channelShown(ch);
        const payload = {}, errors = {};
        let below = null;
        for (const f of channelFields(ch)) {
            const raw = String((typed || {})[f.k] ?? shown[f.k]).trim();
            if (raw !== '') {
                const e = numErr(f.k, raw);
                if (e) { errors[f.k] = e; continue; }
                if (f.k === 'min_interval_s' && f.tpl > 0 && Number(raw) < f.tpl) below = { value: Number(raw), tpl: f.tpl };
            }
            if (raw !== shown[f.k]) payload[f.k] = raw === '' ? null : Number(raw);
        }
        return { payload, errors, below, dirty: !isEmpty(payload) };
    }

    // a binding is edited for the template's own commands; an alias runs its
    // target (its binding applies), and a command with nothing to release
    // (no parameters, no safe value) has no dead-man to arm
    function editable(cmd) {
        if (!cmd || cmd.alias || cmd.from_template === false) return false;
        return !isEmpty(cmd.params) || !isEmpty(cmd.template_safe);
    }

    function safeKeys(cmd) {
        if (!isEmpty(cmd.template_safe)) return Object.keys(cmd.template_safe);
        if (cmd.safe_from === 'device' && !isEmpty(cmd.safe)) return Object.keys(cmd.safe);
        const params = Object.keys(cmd.params || {});
        return params.includes('value') ? ['value'] : params;
    }

    function bindingShown(cmd) {
        const own = cmd.safe_from === 'device' ? (cmd.safe || {}) : {};
        const safe = {};
        for (const k of safeKeys(cmd)) safe[k] = text(own[k]);
        return { lease_s: cmd.lease_s ? String(cmd.lease_s) : '', safe };
    }

    function safeErr(p, raw) {
        const v = Number(raw);
        if (!/^-?\d+(\.\d+)?$/.test(raw) || !Number.isFinite(v)) return { code: 'number' };
        if (p && Array.isArray(p.allowed) && p.allowed.length && !p.allowed.some(a => Number(a) === v))
            return { code: 'allowed', p: { values: p.allowed.join(', ') } };
        if (p && has(p.min) && v < Number(p.min)) return { code: 'below', p: { min: p.min } };
        if (p && has(p.max) && v > Number(p.max)) return { code: 'above', p: { max: p.max } };
        return null;
    }

    // typed: {lease_s: text, safe: {k: text}}. The safe value typed is laid
    // over the template's (a field left empty keeps the template's); every
    // field empty leaves the template's alone (null on the wire)
    function readBinding(cmd, typed) {
        const shown = bindingShown(cmd);
        typed = typed || {};
        const errors = {}, payload = {};
        const lease = String(typed.lease_s ?? shown.lease_s).trim();
        let leaseN = 0;
        if (lease !== '') {
            leaseN = Number(lease);
            if (!/^\d+$/.test(lease) || leaseN > MAX_S) errors.lease_s = { code: 'wholeRange', p: { min: 0, max: MAX_S } };
        }
        const ts = Object.assign({}, shown.safe, typed.safe || {});
        const own = {};
        let safeChanged = false;
        for (const k of Object.keys(ts)) {
            const raw = String(ts[k]).trim();
            if (raw !== shown.safe[k]) safeChanged = true;
            if (raw === '') continue;
            const e = safeErr((cmd.params || {})[k], raw);
            if (e) { errors['safe.' + k] = e; continue; }
            own[k] = Number(raw);
        }
        const typedSafe = isEmpty(own) ? null : Object.assign({}, cmd.template_safe || {}, own);
        const effective = typedSafe || (isEmpty(cmd.template_safe) ? null : cmd.template_safe);
        if (lease !== shown.lease_s) payload.lease_s = lease === '' ? null : leaseN;
        if (safeChanged) payload.safe = typedSafe;
        const dirty = !isEmpty(payload);
        // a dead-man with nothing to release never fires: refused when this
        // edit would store it, said (not blocking) when it is what is stored
        let note = null;
        if (!errors.lease_s && leaseN > 0 && isEmpty(effective) && !Object.keys(errors).some(k => k.startsWith('safe.'))) {
            if (dirty) errors.lease_s = { code: 'leaseNoSafe' }; else note = 'leaseNoSafe';
        }
        return { payload, errors, dirty, safe: effective, note };
    }

    // an installation group's bindings: each ticked command enabled, an
    // unticked one that says something (a lease, a safe value, faces) kept
    // switched off — as a device's own binding is — and the rest dropped.
    // state: {name: {ticked, typed}}
    function groupCommands(cmds, state) {
        const out = [], errors = {};
        for (const c of cmds || []) {
            const st = (state || {})[c.name];
            if (!st) { if (c.binding) out.push(c.binding); continue; }
            const b = Object.assign({}, c.binding || {}, { name: c.name, enabled: !!st.ticked });
            if (editable(c) && st.typed) {
                const r = readBinding(c, st.typed);
                if (!isEmpty(r.errors)) errors[c.name] = r.errors;
                for (const [k, v] of Object.entries(r.payload)) {
                    if (v === null) delete b[k]; else b[k] = v;
                }
            }
            if (!b.enabled && !Object.keys(b).some(k => !['name', 'enabled'].includes(k))) continue;
            out.push(b);
        }
        return { commands: out, errors };
    }

    // its channel overrides: what is stored, with each edited channel's
    // typed values over it (orphans and untouched channels kept as they are).
    // typed: {cid: {k: text}}
    function groupChannels(stored, chs, typed) {
        const out = {};
        for (const [k, v] of Object.entries(stored || {})) out[k] = Object.assign({}, v);
        const errors = {};
        for (const ch of chs || []) {
            const t = (typed || {})[ch.cid];
            if (!t) continue;
            const r = readChannel(ch, t);
            if (!isEmpty(r.errors)) { errors[ch.cid] = r.errors; continue; }
            const next = Object.assign({}, out[ch.key] || {});
            for (const [k, v] of Object.entries(r.payload)) {
                if (v === null) delete next[k]; else next[k] = v;
            }
            if (isEmpty(next)) delete out[ch.key]; else out[ch.key] = next;
        }
        return { channels: out, errors };
    }

    const api = { channels, channelFields, channelShown, readChannel, editable, safeKeys,
                  bindingShown, readBinding, groupCommands, groupChannels, DEFAULT_MAX_AGE_S, MAX_S };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.CmdBindingModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
