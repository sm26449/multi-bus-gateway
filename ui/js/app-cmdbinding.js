// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
//
// command bindings — augments JanitzaMonitor.prototype
//
// What a device (or an installation's group) says of the commands its
// template offers, beyond "enabled": the dead-man (auto-revert after N
// seconds without a renewal, to a safe value) and the pace of each channel
// (min interval, max age; a motor's min on/off and starts per hour). The
// template's values are the defaults, shown beside every field; a value of
// the device's own overrides them for this device only (ADR-0019). The
// numbers are judged by ui/js/cmd-binding-model.js, the same model for the
// device's Commands tab and the group editor.

Object.assign(JanitzaMonitor.prototype, {

    // an element id from a prefix and names that may hold any character
    _cbId(...parts) {
        return parts.map(p => String(p).replace(/[^A-Za-z0-9_-]/g, '_')).join('_');
    },

    // the commands a form belongs to: the device page's or the group editor's
    _cbCmdsOf(scope) {
        return scope === 'grp' ? (this._grpCmds || []) : (this._devCmds || []);
    },

    // a model error in words
    _cbErrText(e) {
        const t = (k, d, p) => this.t(k, d, p);
        const p = e.p || {};
        switch (e.code) {
        case 'number': return t('commands.notNumber', 'must be a number');
        case 'whole': return t('cmdb.err.whole', 'a whole number');
        case 'wholeRange': return t('cmdb.err.wholeRange', 'whole seconds, {min} to {max}', p);
        case 'range': return t('cmdb.err.range', 'from {min} to {max}', p);
        case 'maxAge': return t('cmdb.err.maxAge', 'from {min} to {max} — 0 would expire every command', p);
        case 'below': return `${t('commands.below', 'below the minimum')} ${p.min}`;
        case 'above': return `${t('commands.above', 'above the maximum')} ${p.max}`;
        case 'allowed': return t('cmdb.err.allowed', 'one of {values}', p);
        case 'leaseNoSafe': return t('cmdb.err.leaseNoSafe', 'Auto-revert needs a safe value — without one the dead-man never fires.');
        default: return String(e.code || e);
        }
    },

    _cbFieldLabel(k) {
        const t = (key, d) => this.t(key, d);
        return {
            min_interval_s: t('cmdb.f.minInterval', 'Min interval (s)'),
            max_age_s: t('cmdb.f.maxAge', 'Max age (s)'),
            min_on_s: t('cmdb.f.minOn', 'Min on time (s)'),
            min_off_s: t('cmdb.f.minOff', 'Min off time (s)'),
            max_starts_per_h: t('cmdb.f.maxStarts', 'Max starts per hour'),
        }[k] || k;
    },

    _cbFieldHint(k, f, ch, scope) {
        const t = (key, d, p) => this.t(key, d, p);
        const tplSays = k === 'max_age_s' && !(ch.tpl && ch.tpl.max_age_s != null)
            ? t('cmdb.gatewayDefault', 'gateway default: {v}', { v: f.tpl })
            : t('cmdb.templateSays', 'template: {v}', { v: f.tpl });
        const what = {
            min_interval_s: scope === 'grp'
                ? t('cmdb.h.minIntervalGrp', 'Overrides the channel\'s protection for this group\'s units only. Empty: the template\'s.')
                : t('cmdb.h.minInterval', 'Overrides the channel\'s protection for this device only. Empty: the template\'s.'),
            max_age_s: t('cmdb.h.maxAge', 'A command still waiting for the bus after this long is dropped, not written.'),
            min_on_s: t('cmdb.h.minOn', 'Once started, not stopped before this.'),
            min_off_s: t('cmdb.h.minOff', 'Once stopped, not started again before this.'),
            max_starts_per_h: t('cmdb.h.maxStarts', 'Starts allowed in any hour.'),
        }[k] || '';
        return `${what} (${tplSays})`;
    },

    // the "set in the installation's group" pointer of an installation's unit
    _cbPointerHtml(ctx) {
        const t = (k, d) => this.t(k, d);
        return `<div class="field-hint cb-pointer" data-cb-pointer><i aria-hidden="true" class="bi bi-diagram-3"></i>
            ${t('cmdb.inGroup', 'Set in the installation\'s group')} —
            <a href="#" ${this._act('cbOpenGroup', [ctx.endpointId || '', ctx.groupId || ''])}>${t('cmdb.openGroup', 'open the group editor')}</a></div>`;
    },

    // one channel's pace. ctx: {scope: 'dev'|'grp', prefix, readOnly, pointer
    // (false: read-only without the "set in the group" pointer — a device's view),
    // devId, endpointId, groupId}
    _cbChannelHtml(ch, ctx) {
        const M = window.CmdBindingModel;
        const t = (k, d, p) => this.t(k, d, p);
        const e = v => this._esc(v);
        const ro = !!ctx.readOnly;
        const shown = M.channelShown(ch);
        const fields = M.channelFields(ch);
        const fid = k => this._cbId(ctx.prefix, 'c', ch.cid, k);
        const head = this._cbId(ctx.prefix, 'c', ch.cid, 'title');
        const input = f => {
            const id = fid(f.k);
            const ext = f.k === 'min_interval_s' ? ` ${id}-below` : '';
            return `<div class="form-group cb-field">
                <label class="form-label" for="${e(id)}">${e(this._cbFieldLabel(f.k))}</label>
                <input id="${e(id)}" class="input input-sm" type="text" inputmode="decimal" autocomplete="off"
                       data-cb-field="${e(f.k)}" value="${e(shown[f.k])}" placeholder="${e(f.tpl)}"
                       aria-describedby="${e(id)}-hint${ext}" ${ro ? 'disabled' : ''}>
                <div class="field-hint" id="${e(id)}-hint">${e(this._cbFieldHint(f.k, f, ch, ctx.scope))}</div></div>`;
        };
        const main = fields.filter(f => !f.adv);
        const adv = fields.filter(f => f.adv);
        const names = ch.cmds.map(c => `<code>${e(c.name)}</code>`).join(' ');
        return `<div class="cb-form cb-chan" role="group" aria-labelledby="${e(head)}" data-cb-kind="chan"
                    data-cb-scope="${e(ctx.scope)}" data-cb-cid="${e(ch.cid)}" ${ro ? '' : this._act('cbInput', [], { el: true, on: 'input' })}>
            <div class="cb-head" id="${e(head)}"><i aria-hidden="true" class="bi bi-speedometer2"></i>
                <b>${e(t('cmdb.chanTitle', 'Channel {name}', { name: ch.label || ch.cid }))}</b>
                <span class="field-hint">${names}</span></div>
            <div class="cb-row">${main.map(input).join('')}</div>
            <div class="cb-below field-hint canon-warn" id="${e(fid('min_interval_s'))}-below" data-cb-below aria-live="polite"></div>
            <details class="cb-adv" ${adv.some(f => shown[f.k] !== '') ? 'open' : ''}><summary class="field-hint">${t('cmdb.advanced', 'Advanced')}</summary>
                <div class="cb-row">${adv.map(input).join('')}</div></details>
            ${ro && ctx.pointer !== false ? this._cbPointerHtml(ctx) : ''}
            ${ctx.scope === 'dev' && !ro && !ctx.staged ? `<div class="cb-actions">
                <button type="button" class="btn btn-primary btn-sm" data-cb-save disabled
                    ${this._act('cbSaveChannel', [ctx.devId, ch.cid], { el: true })}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('cmdb.saveChannel', 'Save channel')}</button>
                <span class="cb-out field-hint" role="status" aria-live="polite"></span></div>` : ''}
        </div>`;
    },

    // a command's auto-revert and safe value, beside its Enabled tick
    _cbBindingHtml(c, ctx) {
        const M = window.CmdBindingModel;
        const t = (k, d, p) => this.t(k, d, p);
        const e = v => this._esc(v);
        if (c.alias) {
            return `<div class="field-hint cb-alias">${e(t('cmdb.aliasHint', 'Runs {target}: its auto-revert and safe value apply.', { target: c.alias.command }))}</div>`;
        }
        if (!M.editable(c)) return '';
        const ro = !!ctx.readOnly;
        const shown = M.bindingShown(c);
        const fid = (...p) => this._cbId(ctx.prefix, 'b', c.name, ...p);
        const leaseId = fid('lease');
        const tplSafe = c.template_safe || {};
        const keys = M.safeKeys(c);
        const safeIn = k => {
            const id = fid('safe', k);
            const p = (c.params || {})[k] || {};
            const label = keys.length > 1
                ? `${t('cmdb.safe', 'Safe value')} · ${p.label || k}`
                : t('cmdb.safe', 'Safe value');
            const unit = p.unit ? ` (${p.unit})` : '';
            const grp = ctx.scope === 'grp';
            const from = c.safe_from === 'device'
                ? (tplSafe[k] != null
                    ? (grp ? t('cmdb.safeOwnTplGrp', 'this group\'s — template: {v}', { v: tplSafe[k] })
                           : t('cmdb.safeOwnTpl', 'this device\'s — template: {v}', { v: tplSafe[k] }))
                    : (grp ? t('cmdb.safeOwnGrp', 'this group\'s — the template has none')
                           : t('cmdb.safeOwn', 'this device\'s — the template has none')))
                : c.safe_from === 'template' ? t('cmdb.safeTpl', 'empty: the template\'s, {v}', { v: tplSafe[k] ?? '—' })
                    : t('cmdb.safeNone', 'the template has none');
            return `<div class="form-group cb-field">
                <label class="form-label" for="${e(id)}">${e(label + unit)}</label>
                <input id="${e(id)}" class="input input-sm" type="text" inputmode="decimal" autocomplete="off"
                       data-cb-field="safe.${e(k)}" value="${e(shown.safe[k])}" placeholder="${e(tplSafe[k] ?? '')}"
                       aria-describedby="${e(id)}-hint" ${ro ? 'disabled' : ''}>
                <div class="field-hint" id="${e(id)}-hint" title="${e(t('devtpl.tip.safe', 'Auto-revert value on lease expiry'))}">${e(from)}</div></div>`;
        };
        const cnt = c.counters || {};
        const pills = [
            ['superseded', t('cmdb.cnt.superseded', 'superseded {n}', { n: cnt.superseded }), t('cmdb.cnt.supersededHint', 'Replaced by a newer value of the same setting before it was written.')],
            ['expired', t('cmdb.cnt.expired', 'expired {n}', { n: cnt.expired }), t('cmdb.cnt.expiredHint', 'Waited longer than the channel\'s max age for the bus: dropped, nothing written.')],
            ['held', t('cmdb.cnt.held', 'held {n}', { n: cnt.held }), t('cmdb.cnt.heldHint', 'Refused because the channel\'s min interval had not passed.')]]
            .filter(([k]) => Number(cnt[k]) > 0)
            .map(([k, w, hint]) => `<span class="sink-pill off" data-cb-counter="${k}" title="${e(hint)}">${e(w)}</span>`).join(' ');
        const head = fid('title');
        return `<div class="cb-form cb-bind" role="group" aria-labelledby="${e(head)}" data-cb-kind="bind"
                    data-cb-scope="${e(ctx.scope)}" data-cb-name="${e(c.name)}" ${ro ? '' : this._act('cbInput', [], { el: true, on: 'input' })}>
            <div class="cb-head"><span id="${e(head)}" class="field-hint"><i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> ${e(t('cmdb.bindingFor', 'Dead-man of {name}', { name: c.name }))}</span>
                ${pills ? `<span class="cb-counters" aria-label="${e(t('cmdb.cnt.title', 'Since start'))}">${pills}</span>` : ''}</div>
            <div class="cb-row">
                <div class="form-group cb-field">
                    <label class="form-label" for="${e(leaseId)}">${t('write.lease', 'Auto-revert (s)')}</label>
                    <input id="${e(leaseId)}" class="input input-sm" type="text" inputmode="numeric" autocomplete="off"
                           data-cb-field="lease_s" value="${e(shown.lease_s)}" placeholder="0"
                           aria-describedby="${e(leaseId)}-hint" ${ro ? 'disabled' : ''}>
                    <div class="field-hint" id="${e(leaseId)}-hint">${t('cmdb.h.lease', 'Without a renewal for this long, the safe value is written. Empty or 0: off.')}</div></div>
                ${keys.map(safeIn).join('')}
            </div>
            <div class="cb-note field-hint canon-warn" data-cb-note aria-live="polite"></div>
            ${ro && ctx.pointer !== false ? this._cbPointerHtml(ctx) : ''}
            ${ctx.scope === 'dev' && !ro && !ctx.staged ? `<div class="cb-actions">
                <button type="button" class="btn btn-primary btn-sm" data-cb-save disabled
                    ${this._act('cbSaveBinding', [ctx.devId, c.name], { el: true })}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('cmdb.saveBinding', 'Save')}</button>
                <span class="cb-out field-hint" role="status" aria-live="polite"></span></div>` : ''}
        </div>`;
    },

    // what a form's inputs hold: {k: text} for a channel, {lease_s, safe: {k}}
    // for a binding
    _cbTyped(form) {
        const out = {};
        form.querySelectorAll('[data-cb-field]').forEach(inp => {
            const k = inp.dataset.cbField;
            if (k.startsWith('safe.')) (out.safe = out.safe || {})[k.slice(5)] = inp.value;
            else out[k] = inp.value;
        });
        return out;
    },

    _cbInputOf(form, path) {
        return [...form.querySelectorAll('[data-cb-field]')].find(i => i.dataset.cbField === path) || null;
    },

    // errors at their inputs, without stealing the focus while typing
    _cbShowErrors(form, errors) {
        form.querySelectorAll('[data-cb-field]').forEach(inp => {
            const id = `${inp.id}-err`;
            const ids = (inp.getAttribute('aria-describedby') || '').split(/\s+/).filter(x => x && x !== id);
            const old = document.getElementById(id);
            const msg = errors[inp.dataset.cbField];
            if (!msg) {
                if (old) old.remove();
                inp.removeAttribute('aria-invalid');
                if (ids.length) inp.setAttribute('aria-describedby', ids.join(' '));
                else inp.removeAttribute('aria-describedby');
                return;
            }
            const box = old || Object.assign(document.createElement('div'), { id, className: 'field-error' });
            box.textContent = msg;
            if (!old) inp.closest('.form-group').appendChild(box);
            inp.setAttribute('aria-invalid', 'true');
            inp.setAttribute('aria-describedby', ids.concat(id).join(' '));
        });
    },

    // judge a form as typed: errors, the warnings, its Save. Returns the
    // model's reading, or null when the form's command is gone
    _cbUpdate(form, keepErrors = false) {
        const M = window.CmdBindingModel;
        const t = (k, d, p) => this.t(k, d, p);
        const cmds = this._cbCmdsOf(form.dataset.cbScope);
        const typed = this._cbTyped(form);
        let r;
        if (form.dataset.cbKind === 'chan') {
            const ch = M.channels(cmds).find(x => x.cid === form.dataset.cbCid);
            if (!ch) return null;
            r = M.readChannel(ch, typed);
            const inp = this._cbInputOf(form, 'min_interval_s');
            const warn = form.querySelector('[data-cb-below]');
            if (inp) inp.classList.toggle('cb-below-input', !!r.below);
            if (warn) warn.textContent = r.below ? t('cmdb.belowTpl',
                'Faster than the template\'s {tpl} s — this device\'s channel takes a write every {v} s.', { tpl: r.below.tpl, v: r.below.value }) : '';
        } else {
            const c = cmds.find(x => x.name === form.dataset.cbName);
            if (!c) return null;
            r = M.readBinding(c, typed);
            const note = form.querySelector('[data-cb-note]');
            if (note) note.textContent = r.note ? this._cbErrText({ code: r.note }) : '';
        }
        const errs = {};
        for (const [k, v] of Object.entries(r.errors)) errs[k] = this._cbErrText(v);
        // after a refusal the server's words stay at their fields until typed over
        if (!keepErrors) this._cbShowErrors(form, errs);
        const save = form.querySelector('[data-cb-save]');
        if (save && !save.dataset.busy) save.disabled = !r.dirty || Object.keys(r.errors).length > 0;
        return r;
    },

    cbInput(form) { this._cbUpdate(form); },

    // every form under a host judged once (after a render)
    _cbSync(host) {
        (host || document).querySelectorAll('.cb-form').forEach(f => this._cbUpdate(f));
    },

    // what was typed and not saved, kept across a redraw of the tab (the
    // Enabled tick redraws it)
    _cbKeep(host) {
        const typed = [];
        (host || document).querySelectorAll('.cb-form [data-cb-field]').forEach(inp => {
            if (inp.value !== inp.defaultValue) typed.push([inp.id, inp.value]);
        });
        return () => {
            for (const [id, v] of typed) {
                const inp = document.getElementById(id);
                if (inp && !inp.disabled) inp.value = v;
            }
            this._cbSync(host);
        };
    },

    // a refusal's fields at their inputs ({lease_s: why, safe: why, …}); what
    // has no input here is returned
    _cbServerErrors(form, fields) {
        const errs = {}, left = [];
        for (const [k, v] of Object.entries(fields || {})) {
            const inp = this._cbInputOf(form, k)
                || (k === 'safe' ? form.querySelector('[data-cb-field^="safe."]') : null);
            if (inp) errs[inp.dataset.cbField] = v; else left.push(`${k}: ${v}`);
        }
        this._cbShowErrors(form, errs);
        const first = Object.keys(errs)[0] && this._cbInputOf(form, Object.keys(errs)[0]);
        if (first) { first.closest('details')?.setAttribute('open', ''); first.focus(); }
        return left;
    },

    async _cbPut(form, btn, devId, url, payload, done) {
        const out = form.querySelector('.cb-out');
        const t = (k, d) => this.t(k, d);
        btn.disabled = true;
        btn.dataset.busy = '1';
        if (out) out.textContent = t('common.saving', 'Saving...');
        let ok = false;
        try {
            const r = await window.DeviceStore.devWrite(devId, 'PUT', url, payload);     // ADR-0030
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                const det = d.detail || {};
                const left = det.fields ? this._cbServerErrors(form, det.fields) : (det.errors || [typeof det === 'string' ? det : r.statusText]);
                if (out) out.textContent = left.length ? left.join(' · ') : t('cmdb.refused', 'Refused — see the fields.');
            } else {
                ok = true;
                // saved: what the form holds is no longer "typed" for the redraw
                form.querySelectorAll('[data-cb-field]').forEach(inp => { inp.defaultValue = inp.value; });
                await done(d);
            }
        } catch (e) {
            if (out) out.textContent = String(e.message || e);
        } finally {
            delete btn.dataset.busy;
            if (!ok && btn.isConnected) this._cbUpdate(form, true);
        }
        return ok;
    },

    async cbSaveBinding(devId, name, btn) {
        const form = btn.closest('.cb-form');
        const r = form && this._cbUpdate(form);
        if (!r || !r.dirty || Object.keys(r.errors).length) return;
        const t = (k, d) => this.t(k, d);
        await this._cbPut(form, btn, devId, `/api/devices/${encodeURIComponent(devId)}/commands/${encodeURIComponent(name)}`, r.payload,
            async () => {
                this.showToast('success', t('commands.tab', 'Commands'), `${name}: ${t('cmdb.saved', 'saved')}`);
                if (this._devDetail?.id === devId) await this._initDeviceCommands(devId);
            });
    },

    async cbSaveChannel(devId, cid, btn) {
        const form = btn.closest('.cb-form');
        const r = form && this._cbUpdate(form);
        if (!r || !r.dirty || Object.keys(r.errors).length) return;
        const ch = window.CmdBindingModel.channels(this._devCmds || []).find(x => x.cid === cid);
        if (!ch) return;
        const t = (k, d) => this.t(k, d);
        await this._cbPut(form, btn, devId, `/api/devices/${encodeURIComponent(devId)}/channels/${encodeURIComponent(ch.key)}`, r.payload,
            async () => {
                this.showToast('success', t('commands.tab', 'Commands'), `${ch.label || ch.cid}: ${t('cmdb.saved', 'saved')}`);
                if (this._devDetail?.id === devId) await this._initDeviceCommands(devId);
            });
    },

    // ── the full-screen Commands & bindings editor (ADR-0032 §6, L3.11b) ──
    // Enabled ticks, dead-man bindings and channel paces are staged; the edit
    // bar's one Save writes each changed one by its route, in order — the
    // ticks, the bindings, the channels — and says what was saved.
    _cmdStagedNow(id) {
        return !!(this._ddvEditing === 'commands' && this._ddvFullEditor === 'commands' && this._view?.type === 'device'
                  && this._devDetail?.id === id);
    },

    _cmdStage(name, on) {
        this._cmdStaged = this._cmdStaged || {};
        const c = (this._devCmds || []).find(x => x.name === name);
        if (c && !!c.enabled === !!on) delete this._cmdStaged[name];
        else this._cmdStaged[name] = !!on;
        const fb = document.getElementById('ddvEditFeedback');
        if (fb) { fb.textContent = this.t('ddv.full.staged', 'Not saved yet — Save writes it.'); fb.className = 'save-feedback'; }
    },

    _cmdForms() {
        return [...document.querySelectorAll('#deviceDetailView [data-dpanel="commands"] .cb-form[data-cb-scope="dev"]')];
    },

    _cmdDirty() {
        if (Object.keys(this._cmdStaged || {}).length) return true;
        return this._cmdForms().some(f => (this._cbUpdate(f, true) || {}).dirty);
    },

    // the editor's Save: {saved, failed, skipped} (each named by its command or channel)
    async _cmdSaveStaged() {
        const id = this._devDetail?.id;
        const t = (k, d, p) => this.t(k, d, p);
        const rep = { saved: [], failed: null, skipped: [] };
        if (!id) return rep;
        const M = window.CmdBindingModel;
        const steps = [];
        for (const [name, on] of Object.entries(this._cmdStaged || {})) {
            steps.push({ label: on ? t('ddv.full.cmdEnabled', '{name} enabled', { name }) : t('ddv.full.cmdDisabled', '{name} disabled', { name }), form: null,
                         url: `/api/devices/${encodeURIComponent(id)}/commands/${encodeURIComponent(name)}/enabled`, body: { enabled: on }, tick: name });
        }
        // every changed form judged first: nothing is written while one is wrong
        for (const form of this._cmdForms()) {
            const r = this._cbUpdate(form);
            if (!r || !r.dirty) continue;
            if (Object.keys(r.errors).length) {
                rep.failed = { id: form.dataset.cbName || form.dataset.cbCid, error: t('cmdb.refused', 'Refused — see the fields.') };
                rep.skipped = steps.map(s => s.label);
                return rep;
            }
            if (form.dataset.cbKind === 'chan') {
                const ch = M.channels(this._devCmds || []).find(x => x.cid === form.dataset.cbCid);
                if (!ch) continue;
                steps.push({ label: t('cmdb.chanTitle', 'Channel {name}', { name: ch.label || ch.cid }), form,
                             url: `/api/devices/${encodeURIComponent(id)}/channels/${encodeURIComponent(ch.key)}`, body: r.payload });
            } else {
                const name = form.dataset.cbName;
                steps.push({ label: t('cmdb.bindingFor', 'Dead-man of {name}', { name }), form,
                             url: `/api/devices/${encodeURIComponent(id)}/commands/${encodeURIComponent(name)}`, body: r.payload });
            }
        }
        for (const st of steps) {
            if (rep.failed) { rep.skipped.push(st.label); continue; }
            let r, d = {};
            try {
                r = await window.DeviceStore.devWrite(id, 'PUT', st.url, st.body);     // ADR-0030
                d = await r.json().catch(() => ({}));
            } catch (e) {
                rep.failed = { id: st.label, error: String(e.message || e) };
                continue;
            }
            if (!r.ok) {
                const det = d.detail || {};
                const left = st.form && det.fields ? this._cbServerErrors(st.form, det.fields) : (det.errors || [this._respError(r, d)]);
                rep.failed = { id: st.label, error: left.length ? left.join(' · ') : t('cmdb.refused', 'Refused — see the fields.') };
                continue;
            }
            rep.saved.push(st.label);
            if (st.tick) delete this._cmdStaged[st.tick];
            // saved: what the form holds is no longer "typed" for the redraw
            if (st.form) st.form.querySelectorAll('[data-cb-field]').forEach(inp => { inp.defaultValue = inp.value; });
        }
        await this._fetchDevices?.(true);
        // drawn again from what is stored — not after a refusal, whose words
        // stay at their fields (what was typed is kept either way)
        if (!rep.failed && this._devDetail?.id === id) await this._initDeviceCommands(id);
        return rep;
    },

    // from an installation's unit to the editor of its group
    async cbOpenGroup(endpointId, groupId) {
        if (!endpointId) return;
        if (await this.openEndpointDetail(endpointId) === false) return;
        if (groupId) await this.openGroupModal(endpointId, groupId);
    },

    // ── the installation group's editor ──
    //
    // The same fields, saved with the group (its bindings and its channel
    // overrides apply to every unit of it).

    _cbGroupHtml(g) {
        const M = window.CmdBindingModel;
        const ctx = { scope: 'grp', prefix: 'gcb' };
        return M.channels(g.commands || []).map(ch => `<div class="cb-group" data-channel-group="${this._esc(ch.cid)}">
            ${this._cbChannelHtml(ch, ctx)}
            ${ch.cmds.map(c => {
                const b = this._cbBindingHtml(c, ctx);
                return b ? `<div class="cb-grp-cmd" data-cb-cmd="${this._esc(c.name)}"><div class="field-hint"><code>${this._esc(c.name)}</code></div>${b}</div>` : '';
            }).join('')}
        </div>`).join('');
    },

    // what the group editor's fields make of the group's commands and
    // channels: {commands, channels, errors: {inputId: why}}
    _cbGroupRead(g, ticked) {
        const M = window.CmdBindingModel;
        const modal = document.getElementById('endpointModal');
        const state = {};
        for (const c of g.commands || []) {
            const form = modal?.querySelector(`.cb-bind[data-cb-scope="grp"][data-cb-name="${CSS.escape(c.name)}"]`);
            state[c.name] = { ticked: ticked.has(c.name), typed: form ? this._cbTyped(form) : null };
        }
        const chs = M.channels(g.commands || []);
        const typedCh = {};
        for (const ch of chs) {
            const form = modal?.querySelector(`.cb-chan[data-cb-scope="grp"][data-cb-cid="${CSS.escape(ch.cid)}"]`);
            if (form) typedCh[ch.cid] = this._cbTyped(form);
        }
        const cmds = M.groupCommands(g.commands || [], state);
        const chans = M.groupChannels(g.channels || {}, chs, typedCh);
        const errors = {};
        for (const [name, errs] of Object.entries(cmds.errors)) {
            for (const [k, v] of Object.entries(errs)) {
                const id = k === 'lease_s' ? this._cbId('gcb', 'b', name, 'lease') : this._cbId('gcb', 'b', name, 'safe', k.slice(5));
                errors[id] = this._cbErrText(v);
            }
        }
        for (const [cid, errs] of Object.entries(chans.errors)) {
            for (const [k, v] of Object.entries(errs)) errors[this._cbId('gcb', 'c', cid, k)] = this._cbErrText(v);
        }
        return { commands: cmds.commands, channels: chans.channels, errors };
    },

    // a server path about the group's bindings or channels → its input
    // (opened when it sits under a closed Advanced, to be seen)
    _cbGroupFieldId(path, sent, g) {
        const id = this._cbGroupFieldPath(path, sent, g);
        if (id) document.getElementById(id)?.closest('details')?.setAttribute('open', '');
        return id;
    },

    _cbGroupFieldPath(path, sent, g) {
        let m = /^commands\[(\d+)\]\.(lease_s|safe)/.exec(path);
        if (m) {
            const b = (sent.commands || [])[Number(m[1])];
            if (!b) return '';
            if (m[2] === 'lease_s') return this._cbId('gcb', 'b', b.name, 'lease');
            const c = (g.commands || []).find(x => x.name === b.name);
            const k = c ? window.CmdBindingModel.safeKeys(c)[0] : 'value';
            return this._cbId('gcb', 'b', b.name, 'safe', k);
        }
        m = /^channels\.([^.]+)\.(\w+)$/.exec(path);
        if (m) {
            const ch = window.CmdBindingModel.channels(g.commands || []).find(x => x.key === m[1] || x.cid === m[1]);
            return ch ? this._cbId('gcb', 'c', ch.cid, m[2]) : '';
        }
        return '';
    },
});
