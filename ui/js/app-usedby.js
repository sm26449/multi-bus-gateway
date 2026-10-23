// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
//
// used by — augments JanitzaMonitor.prototype
//
// What uses a device, an installation or a template, and the way to it.
// The gateway refuses a delete (409) while a rule, a sequence, another
// device's formula, a virtual meter… still names the thing: this says what,
// links to each, and offers Disable instead.
//
// One thing is one row and one count everywhere (a rule that reads and
// commands a device is one rule): the list badge, the page header and the
// section agree with the gateway's own count.
//
//   _ubMount(kind, id, host, ctx)  the "Used by" section, loaded into host
//   _ubCountsLater(paint)          {id: n} for a list, painted when it arrives
//   _ubDelete({kind, id, …})       a delete that asks the gateway first
//   usedByOpen(type, id, tab, where)  opens the exact place a dependent lives

Object.assign(JanitzaMonitor.prototype, {

    // how dependents are grouped on the page: one section per kind of thing
    // (icon, label key, fallback); a rule that reads and commands is one rule
    _UB_SECTIONS: {
        rule:             ['bi-sliders', 'usedby.sec.rule', 'Rules'],
        sequence:         ['bi-list-ol', 'usedby.sec.sequence', 'Sequences'],
        calculated:       ['bi-calculator', 'usedby.sec.calculated', 'Calculated values on other devices'],
        meter_link:       ['bi-speedometer2', 'usedby.sec.meter_link', 'Devices it measures'],
        virtual_meter:    ['bi-hdd-network', 'usedby.sec.virtual_meter', 'Virtual meters'],
        template_formula: ['bi-file-earmark-code', 'usedby.sec.template_formula', 'Template formulas'],
        installation:     ['bi-diagram-3', 'usedby.sec.installation', 'Installations'],
        device:           ['bi-cpu', 'usedby.sec.device', 'Devices'],
        component:        ['bi-box', 'usedby.sec.component', 'Components'],
        attribute:        ['bi-tags', 'usedby.sec.attribute', 'Attribute entries'],
        calculated_preset: ['bi-bookmark', 'usedby.sec.calculated_preset', 'Saved formula presets'],
    },
    _UB_SECTION_OF: {
        rule_target: 'rule', rule_input: 'rule', sequence_step: 'sequence',
        installation_group: 'installation', installation_source: 'installation',
    },

    _ubUrl(kind, id) {
        const e = encodeURIComponent(id);
        return kind === 'installation' ? `/api/endpoints/${e}`
            : kind === 'template' ? `/api/device-templates/${e}`
            : `/api/devices/${e}`;
    },

    async _ubLoad(kind, id) {
        const r = await fetch(`${this._ubUrl(kind, id)}/dependents`);
        if (!r.ok) throw new Error(this._respError(r, await r.json().catch(() => ({}))));
        const d = await r.json();
        return { blocking: d.blocking || [], follows: d.follows || [] };
    },

    // the dependents as THINGS: one per (what it is, its id), holding every
    // place it names the object — the unit the gateway counts in too
    _ubThings(list) {
        const by = new Map();
        for (const x of list || []) {
            const ref = x.ref || {};
            const key = `${ref.type || x.type}:${x.id}`;
            if (!by.has(key)) by.set(key, { key, section: this._UB_SECTION_OF[x.type] || x.type,
                                            id: x.id, label: x.label || x.id, ref, entries: [] });
            by.get(key).entries.push(x);
        }
        return [...by.values()];
    },

    _ubCount(deps) {
        return this._ubThings(deps?.blocking).length;
    },

    // ── list badges: drawn later, never waited for ──

    // the counts for every device and installation; paint(map) runs when
    // they arrive, unless the list was left or drawn again meanwhile
    _ubCountsLater(paint) {
        const gen = (this._ubGen = (this._ubGen || 0) + 1);
        fetch('/api/dependents')
            .then(r => (r.ok ? r.json() : {}))
            .then(d => {
                if (gen !== this._ubGen || this.currentPage !== 'devices') return;
                this._ubCountsMap = d.counts || {};
                paint(this._ubCountsMap);
            })
            .catch(() => { /* a badge is a convenience: none rather than a wrong one */ });
    },

    // the slot a row's badge is painted into (drawn with the last known
    // count, so a redraw does not flicker)
    _ubSlotHtml(id, kind = 'device') {
        return `<span class="ub-slot" data-ub-slot="${this._esc(id)}" data-ub-kind="${kind}">${this._ubBadgeInner(id, kind)}</span>`;
    },

    _ubBadgeInner(id, kind) {
        const n = this._ubRowCount(id);
        if (!n) return '';
        const key = kind === 'installation' ? 'usedby.badgeInst' : 'usedby.badge';
        return `<span class="ub-pill" title="${this._esc(this.t('usedby.badgeTitle', 'Changed or removed first, before it can be deleted'))}"><i aria-hidden="true" class="bi bi-link-45deg"></i> ${this._esc(this.tn(key, n, 'used by {n}'))}</span>`;
    },

    // the gateway's own count, as a delete would be refused with: a parent's
    // already holds what uses its components (once — never added again here)
    _ubRowCount(id) {
        return (this._ubCountsMap || {})[id] || 0;
    },

    // fills every slot and holds back the Delete of what is used
    _ubPaintList(root) {
        if (!root) return;
        root.querySelectorAll('[data-ub-slot]').forEach(el => {
            el.innerHTML = this._ubBadgeInner(el.dataset.ubSlot, el.dataset.ubKind);
        });
        root.querySelectorAll('[data-ub-del]').forEach(btn => this._ubHold(btn, this._ubRowCount(btn.dataset.ubDel)));
    },

    // Delete held back while n things use it: still focusable and clickable
    // (it shows what uses it), and the reason is part of its name
    _ubHold(btn, n, whyEl = null) {
        if (!btn) return;
        const del = this.t('common.delete', 'Delete');
        const reason = n ? this.tn('usedby.deleteHeld', n, 'Cannot be deleted while {n} thing(s) use it — see "Used by".') : '';
        // a row's icon button says which row (data-ub-name); the tip shows on focus too
        const name = btn.dataset.ubName ? `${del} ${btn.dataset.ubName}` : del;
        if (n) btn.setAttribute('aria-disabled', 'true'); else btn.removeAttribute('aria-disabled');
        if (btn.hasAttribute('data-tip')) { btn.dataset.tip = n ? reason : del; btn.removeAttribute('title'); }
        else btn.title = n ? reason : del;
        if (whyEl) whyEl.textContent = reason;
        else if (btn.hasAttribute('aria-label')) btn.setAttribute('aria-label', n ? `${name}: ${reason}` : name);
    },

    // ── the section ──

    _ubWhat(kind) {
        return kind === 'installation' ? this.t('usedby.thisInstallation', 'this installation')
            : kind === 'template' ? this.t('usedby.thisTemplate', 'this template')
            : this.t('usedby.thisDevice', 'this device');
    },

    // the one line said to a screen reader when it changes
    _ubStatus(kind, ctx, deps) {
        const n = this._ubCount(deps);
        if (!n) return this.t('usedby.none', 'Nothing uses {what}.', { what: this._ubWhat(kind) });
        const leads = {
            device: ['usedby.lead.device', 'It cannot be deleted while {n} thing(s) use it. Change or remove them first, or disable it instead.'],
            installation: ['usedby.lead.installation', 'It cannot be deleted while {n} thing(s) use it. Change or remove them first, or disable it instead.'],
            template: ['usedby.lead.template', 'It cannot be deleted while {n} thing(s) use it. Move them to another template first.'],
            component: ['usedby.lead.component', 'It cannot be removed while {n} thing(s) use it. Change or remove them first.'],
            unit: ['usedby.lead.unit', '{n} thing(s) use it: its installation cannot drop it while they do.'],
            primary: ['usedby.lead.primary', '{n} thing(s) use it.'],
        };
        const [key, fb] = leads[ctx] || leads[kind] || leads.device;
        return this.tn(key, n, fb);
    },

    _ubHtml(ownId, deps) {
        const t = (k, d, p) => this.t(k, d, p);
        const sections = (things) => {
            const by = new Map();
            things.forEach(x => { if (!by.has(x.section)) by.set(x.section, []); by.get(x.section).push(x); });
            return [...by.entries()].map(([sec, items]) => {
                const [icon, key, fb] = this._UB_SECTIONS[sec] || ['bi-dot', `usedby.sec.${sec}`, sec];
                return `<div class="ub-group">
                    <h4 class="ub-group-h"><i aria-hidden="true" class="bi ${icon}"></i> ${this._esc(t(key, fb))} <span class="ub-count">${items.length}</span></h4>
                    <ul class="ub-list">${items.map(x => this._ubItemHtml(ownId, x)).join('')}</ul></div>`;
            }).join('');
        };
        const follows = this._ubThings(deps.follows);
        const fol = follows.length ? `<details class="ub-follows">
                <summary>${this._esc(this.tn('usedby.follows', follows.length, '{n} thing(s) go with it when it is deleted'))}</summary>
                ${sections(follows)}</details>` : '';
        return `${sections(this._ubThings(deps.blocking))}${fol}`;
    },

    // what a thing does with the object, said once on its row: a rule
    // "commands it · reads it in signal, when"; a calculated value its name
    _ubParts(x) {
        const t = (k, d, p) => this.t(k, d, p);
        if (x.section === 'rule') {
            const out = [];
            if (x.entries.some(e => e.type === 'rule_target')) out.push(t('usedby.commands', 'commands it'));
            const reads = [...new Set(x.entries.filter(e => e.type === 'rule_input').map(e => e.where))];
            if (reads.length) out.push(t('usedby.reads', 'reads it in {where}', { where: reads.join(', ') }));
            return out.join(' · ');
        }
        return [...new Set(x.entries.map(e => this._ubWhere(e)).filter(Boolean))].join(', ');
    },

    _ubWhere(x) {
        const w = String(x.where || '');
        if (x.type === 'calculated' || x.type === 'template_formula') return w.replace(/^calculated\./, '');
        if (x.type === 'sequence_step') {
            const m = w.match(/^(\w+)\[(\d+)\]/);
            return m ? this.t('usedby.step', 'step {n}', { n: +m[2] + 1 }) : w;
        }
        if (x.type === 'meter_link') return w.replace(/^meters\./, '');
        if (x.type === 'installation_group' || x.type === 'installation_source') {
            const g = w.match(/^groups\[(\d+)\]/);
            return g ? this.t('usedby.group', 'group {n}', { n: +g[1] + 1 }) : this.t('usedby.source', 'a source');
        }
        if (x.type === 'installation') return this.t('usedby.instTemplate', 'its template');
        return '';
    },

    _ubItemHtml(ownId, x) {
        const t = (k, d, p) => this.t(k, d, p);
        const ref = x.ref || {};
        const vias = [...new Set(x.entries.map(e => e.of).filter(o => o && o !== ownId))];
        const via = vias.length ? ` <span class="ub-via">${this._esc(t('usedby.via', 'through {id}', { id: vias.join(', ') }))}</span>` : '';
        const parts = this._ubParts(x);
        const what = parts ? ` <span class="ub-where">${this._esc(parts)}</span>` : '';
        const name = `<span class="ub-name">${this._esc(x.label)}</span>${x.label !== x.id ? ` <span class="dev-chip">${this._esc(x.id)}</span>` : ''}`;
        if (!this._ubCanOpen(ref.type)) return `<li><span class="ub-item">${name}</span>${what}${via}</li>`;
        const where = x.entries[0]?.where || '';
        const aria = t(`usedby.open.${ref.type}`, 'Open {id}', { id: x.label });
        return `<li><a href="#" class="ub-item" aria-label="${this._esc(aria)}" ${this._act('usedByOpen', [ref.type, ref.id, ref.tab || '', where])}>${name}</a>${what}${via}</li>`;
    },

    _ubCanOpen(type) {
        return ['rule', 'sequence', 'device', 'installation', 'virtual_meter', 'template'].includes(type);
    },

    // the "Used by" card into host, with its loading and error states;
    // answers the dependents (or null when they could not be read).
    // ctx: what the page offers — device, installation, template (Delete),
    // component (Remove), unit and primary (neither)
    async _ubMount(kind, id, host, ctx = kind) {
        if (!host) return null;
        const t = (k, d, p) => this.t(k, d, p);
        if (!host.querySelector('[data-ub-body]')) {
            host.classList.add('settings-card', 'ub-card');
            host.setAttribute('aria-labelledby', `ubH-${kind}`);
            host.innerHTML = `
                <div class="settings-card-header"><h3 id="ubH-${kind}"><i aria-hidden="true" class="bi bi-diagram-2"></i> ${this._esc(kind === 'installation' ? t('usedby.titleInst', 'Used by') : t('usedby.title', 'Used by'))} <span class="ub-count" data-ub-total></span></h3></div>
                <div class="settings-card-body">
                    <p class="ub-status" role="status" aria-live="polite" data-ub-status>${this._esc(t('common.loading', 'Loading…'))}</p>
                    <div data-ub-body></div>
                </div>`;
        }
        host.dataset.ubCtx = ctx;
        const status = host.querySelector('[data-ub-status]');
        const body = host.querySelector('[data-ub-body]');
        let deps;
        try { deps = await this._ubLoad(kind, id); }
        catch (e) {
            status.className = 'ub-status ub-error';
            status.textContent = t('usedby.loadFail', 'Could not read what uses it: {why}', { why: e.message });
            body.innerHTML = `<button type="button" class="btn btn-ghost btn-sm" data-ub-retry>${this._esc(t('common.retry', 'Try again'))}</button>`;
            body.querySelector('[data-ub-retry]').addEventListener('click', () => this._ubMount(kind, id, host, ctx));
            return null;
        }
        this._ubShow(kind, id, host, deps);
        return deps;
    },

    // draws a known answer (also the one a refused delete brings back)
    _ubShow(kind, id, host, deps) {
        if (!host) return;
        const n = this._ubCount(deps);
        const status = host.querySelector('[data-ub-status]');
        const total = host.querySelector('[data-ub-total]');
        if (total) total.textContent = n ? String(n) : '';
        if (status) {
            status.className = `ub-status ${n ? 'ub-lead' : 'ub-empty'}`;
            status.textContent = this._ubStatus(kind, host.dataset.ubCtx || kind, deps);
        }
        const body = host.querySelector('[data-ub-body]');
        if (body) body.innerHTML = this._ubHtml(id, deps);
    },

    _ubFocus(host) {
        if (!host) return;
        host.scrollIntoView({ block: 'start', behavior: 'smooth' });
        const h = host.querySelector('h3');
        if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
        host.classList.remove('st-highlight');
        void host.offsetWidth;
        host.classList.add('st-highlight');
    },

    // a held Delete asks again before saying "still used": the list on the
    // page may be older than the gateway. Answers true when it is free now.
    async _ubRecheck(kind, id, host, apply) {
        const deps = await this._ubMount(kind, id, host, host?.dataset.ubCtx || kind);
        if (deps && apply) apply(deps);
        if (deps && !this._ubCount(deps)) return true;
        this._ubFocus(host);
        return false;
    },

    // ── delete ──

    // a delete that asks the gateway what it would do first: refused → the
    // section says what uses it; allowed → the dialog names what goes with it.
    // o: {kind, id, url?, host?, title, message, typed?, details?, confirmLabel?}
    async _ubDelete(o) {
        const t = (k, d, p) => this.t(k, d, p);
        const url = o.url || this._ubUrl(o.kind, o.id);
        let r, d;
        try {
            r = await fetch(`${url}?dry_run=true`, { method: 'DELETE' });
            d = await r.json().catch(() => ({}));
        } catch (e) { this._opError(t('common.delete', 'Delete'), e); return false; }
        if (r.status === 409 && d.detail?.dependents) { this._ubRefused(o, d.detail.dependents); return false; }
        if (!r.ok || d.status !== 'valid') {
            this.showToast('error', t('common.delete', 'Delete'), r.ok ? t('usedby.unexpected', 'The gateway gave an answer this page does not understand. Nothing was deleted from here.') : this._respError(r, d));
            return false;
        }
        const follows = (d.dependents || {}).follows || [];
        const comps = follows.filter(x => x.type === 'component');
        const details = [...(o.details || [])];
        // what goes with it, by kind: "Its 2 components go with it: …"
        const said = { component: ['devices.deleteKids', 'Its {n} component(s) go with it: {names}'],
                       device: ['usedby.goesUnits', 'Its {n} unit(s) go with it: {names}'],
                       attribute: ['usedby.goesAttrs', 'Its {n} attribute entr(y/ies) go with it: {names}'],
                       calculated_preset: ['usedby.goesPresets', '{n} saved formula(s) that name it go with it: {names}'] };
        [...new Set(follows.map(x => x.type))].forEach(type => {
            const things = this._ubThings(follows.filter(x => x.type === type));
            const names = things.map(x => x.label);
            const list = names.slice(0, 8).join(', ') + (names.length > 8 ? ', …' : '');
            if (said[type]) { details.push(this.tn(said[type][0], names.length, said[type][1], { names: list })); return; }
            const [, key, fb] = this._UB_SECTIONS[this._UB_SECTION_OF[type] || type] || [null, `usedby.sec.${type}`, type];
            details.push(t('usedby.goesWith', '{what} ({n}) go with it: {names}', { what: t(key, fb), n: names.length, names: list }));
        });
        let raced = null;
        const ok = await this.confirmAction({
            title: o.title, message: o.message || '', details,
            confirmLabel: o.confirmLabel || t('common.delete', 'Delete'), danger: true,
            typed: o.typed || (comps.length ? o.id : null),
            submit: async () => {
                const rr = await fetch(url, { method: 'DELETE' });
                const dd = await rr.json().catch(() => ({}));
                // something started using it after the check: said on the page
                if (rr.status === 409 && dd.detail?.dependents) { raced = dd.detail.dependents; return null; }
                if (rr.ok) { o.result = dd; return null; }
                return this._respError(rr, dd);
            },
        });
        if (!ok) return false;
        if (raced) { this._ubRefused(o, raced); return false; }
        return true;
    },

    // a refused delete: the server's own list, where the user can act on it
    // (focus moves after the dialog has given it back)
    _ubRefused(o, deps) {
        const t = (k, d, p) => this.t(k, d, p);
        o.refused = deps;
        this.showToast('warning', t('usedby.refusedTitle', 'Still in use'),
            this.tn('usedby.refused', this._ubCount(deps), '{n} thing(s) use it — see "Used by".'));
        const host = o.host || null;
        if (host) {
            this._ubShow(o.kind, o.id, host, deps);
            setTimeout(() => this._ubFocus(host), 60);
        }
        else if (o.kind === 'device') this.usedByOpen('device', o.id, 'usedby');
        else if (o.kind === 'installation') this.usedByOpen('installation', o.id, 'usedby');
        else if (o.kind === 'template') this.viewTemplate(o.id);
    },

    // ── Disable instead ──

    // what disabling does to what uses it, said before (tier W): rules that
    // read it go stale and do what their stale policy says; rules that
    // command it are refused their writes; formulas and virtual meters stop
    async _ubDisableEffects(deps) {
        const t = (k, d, p) => this.t(k, d, p);
        let rules = this._rules;
        if (!rules) {
            try { rules = (await (await fetch('/api/rules')).json()).rules || []; } catch (e) { rules = []; }
        }
        const raw = id => { const r = (rules || []).find(x => x.id === id) || {}; return { ...(r.raw || {}), ...r }; };
        const out = [];
        for (const x of this._ubThings(deps.blocking)) {
            if (x.section === 'rule') {
                const r = raw(x.id);
                const live = r.mode === 'armed' && r.enabled !== false;
                const reads = x.entries.some(e => e.type === 'rule_input');
                const cmds = x.entries.some(e => e.type === 'rule_target');
                const os = r.on_stale ?? 'hold';
                const policy = os === 'hold' ? t('usedby.staleHold', 'it holds what it asked last')
                    : os === 'safe' ? t('usedby.staleSafe', 'it sends its safe values')
                    : t('usedby.staleValue', 'it asks for {v}', { v: os });
                const mode = live ? '' : ` (${t('usedby.notArmed', 'not armed: nothing is written')})`;
                if (reads) out.push(t('usedby.effReads', 'Rule {name}: its input goes stale, then {policy}.', { name: x.label, policy }) + mode);
                if (cmds) out.push(t('usedby.effCmds', 'Rule {name}: its writes to it are refused while it is disabled.', { name: x.label }) + mode);
            } else if (x.section === 'sequence') {
                out.push(t('usedby.effSeq', 'Sequence {name}: a run that reaches it fails.', { name: x.label }));
            } else if (x.section === 'calculated' || x.section === 'template_formula') {
                out.push(t('usedby.effCalc', '{name}: its calculated value stops updating.', { name: x.label }));
            } else if (x.section === 'virtual_meter') {
                out.push(t('usedby.effVm', 'Virtual meter {name}: its source goes stale; it falls back to its fail-safe.', { name: x.label }));
            } else if (x.section === 'meter_link') {
                out.push(t('usedby.effMeter', '{name}: what it measures for it stops updating.', { name: x.label }));
            }
        }
        return out;
    },

    async ubDisableInstead(kind, id) {
        const t = (k, d, p) => this.t(k, d, p);
        const isInst = kind === 'installation';
        const url = isInst ? `/api/endpoints/${encodeURIComponent(id)}` : `/api/devices/${encodeURIComponent(id)}`;
        const opener = document.activeElement;
        let deps = isInst ? (this._endpointUsedBy?.id === id ? this._endpointUsedBy.deps : null)
                          : (this._devDetail?.id === id ? this._devDetail.usedBy : null);
        if (!deps) { try { deps = await this._ubLoad(kind, id); } catch (e) { deps = { blocking: [], follows: [] }; } }
        const ok = await this.confirmAction({
            title: isInst ? t('usedby.disableInstTitle', 'Disable installation {id}', { id })
                          : t('usedby.disableDevTitle', 'Disable device {id}', { id }),
            message: isInst ? t('usedby.disableInstMsg', 'All its units stop reading. The references to it stay, and it can be enabled again. What uses it reacts like this:')
                            : t('usedby.disableDevMsg', 'Its reading stops. The references to it stay, and it can be enabled again. What uses it reacts like this:'),
            details: await this._ubDisableEffects(deps),
            confirmLabel: t('usedby.disable', 'Disable'),
            submit: async () => {
                const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                             body: JSON.stringify({ enabled: false }) });
                if (r.ok) return null;
                return this._respError(r, await r.json().catch(() => ({})));
            },
        });
        if (!ok) { if (opener && document.contains(opener)) opener.focus(); return; }
        this.showToast('success', t('usedby.disabledTitle', 'Disabled'), id);
        if (isInst) { if (this.openEndpointDetail) await this.openEndpointDetail(id); }
        else await this.openDeviceDetail(id);
        // the page was drawn again: focus its title, not <body>
        const h = document.querySelector('#deviceDetailView h2');
        if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
    },

    // ── the way to a dependent ──

    // opens the exact place a dependent lives — its editor (at the field)
    // for an administrator, its card (found and highlighted) for anyone else
    async usedByOpen(type, id, tab = '', where = '') {
        const admin = this._isAdminView ? this._isAdminView() : true;
        // from a template's View: the place opens behind the modal otherwise
        if (document.getElementById('tmViewModal')?.classList.contains('active')) this.closeModal('tmViewModal');
        if (type === 'device') {
            await this.openDeviceDetail(id);
            if (this._devDetail?.id !== id) return;
            if (tab === 'usedby') { this._switchDeviceTab('overview'); this._ubFocus(document.getElementById('devUsedBy')); return; }
            const want = (this._DTAB_ALIAS || {})[tab] || tab;
            const btn = want && document.querySelector(`#deviceWsTabs [data-dtab="${CSS.escape(want)}"]`);
            // a tab the account may not open is not opened for it either (by
            // role: a tab of another group is out of sight too)
            if (!btn || (this._dtabAllowed && !this._dtabAllowed(btn))) return;
            this._switchDeviceTab(want);
            if (want === 'calculated' && where.startsWith('calculated.')) {
                this._ubSeek(`#deviceDetailView .calc-row[data-name="${CSS.escape(where.slice(11))}"]`);
            }
            return;
        }
        if (type === 'installation') {
            await this.openEndpointDetail(id);
            if (tab === 'usedby') { this._ubFocus(document.getElementById('plUsedBy')); return; }
            const g = where.match(/^groups\[(\d+)\]/);
            const gid = g && ((this._endpointDetail?.groups || [])[+g[1]] || {}).id;
            if (gid) this._ubSeek(`[data-endpoint-page] [data-group="${CSS.escape(gid)}"]`);
            return;
        }
        if (type === 'template') { this.viewTemplate(id); return; }
        if (type === 'rule' || type === 'sequence') {
            const sel = type === 'rule' ? `[data-rule="${CSS.escape(String(id))}"]` : `[data-sequence="${CSS.escape(String(id))}"]`;
            this._pendingFocus = { sel, until: Date.now() + 60000, done: false };
            this.navigateTo('rules');
            this._applyPendingFocus();
            if (!admin) return;
            const modalId = type === 'rule' ? 'ruleModal' : 'seqModal';
            if (type === 'rule' && this._loadRules) { await this._loadRules(true); await this.openRuleModal(id); }
            if (type === 'sequence' && this._loadSequences) { await this._loadSequences(true); await this.openSequenceModal(id); }
            // closing the editor returns to the card, not to a link that is gone
            // (looked up when it closes: the page may have drawn the card again)
            const m = document.getElementById(modalId);
            if (m) m._returnFocus = { focus: () => {
                const el = document.querySelector(sel);
                if (el) { if (!el.hasAttribute('tabindex')) el.tabIndex = -1; el.focus(); }
            } };
            if (type === 'rule') {
                // after the modal's own first focus (openModal defers it)
                const field = ({ target: 'rlTarget', signal: 'rlSignal', release: 'rlRelease', when: 'rlSchedWhen' })[where];
                // once the editor has drawn and given its own first focus;
                // never taken back from a field the user already moved to
                const first = document.activeElement;
                const put = (tries = 0) => {
                    const el = field && document.getElementById(field);
                    const modal = document.getElementById('ruleModal');
                    if (!el || !modal?.classList.contains('active')) { if (tries < 10) setTimeout(() => put(tries + 1), 50); return; }
                    const a = document.activeElement;
                    if (a !== first && a && modal.contains(a) && a !== modal.querySelector('.modal-content') && tries > 1) return;
                    el.scrollIntoView({ block: 'center' });
                    el.focus({ preventScroll: true });
                    el.classList.add('st-highlight');
                };
                if (field) setTimeout(() => put(0), 80);
            }
            return;
        }
        if (type === 'virtual_meter') {
            const sel = `.vm-acc[data-mid="${CSS.escape(String(id))}"]`;
            this._pendingFocus = { sel, until: Date.now() + 60000, done: false };
            this.navigateTo('vmeters');
            this._applyPendingFocus();
            // opened, not only found
            this._ubSeek(sel, card => {
                const body = card.querySelector('.vm-acc-body');
                if (body && body.hidden) card.querySelector('.vm-acc-head')?.click();
            });
        }
    },

    // waits (briefly) for an element a page draws later, then shows it
    _ubSeek(sel, then = null, tries = 0) {
        const el = document.querySelector(sel);
        if (el) {
            if (then) then(el);
            el.scrollIntoView({ block: 'center' });
            if (!el.hasAttribute('tabindex')) el.tabIndex = -1;
            el.focus({ preventScroll: true });
            el.classList.remove('st-highlight');
            void el.offsetWidth;
            el.classList.add('st-highlight');
            return;
        }
        if (tries < 40) setTimeout(() => this._ubSeek(sel, then, tries + 1), 150);
    },
});
