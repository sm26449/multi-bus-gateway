/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// pickers — augments JanitzaMonitor.prototype
//
// One way to choose a device, a field or a template, wherever the page asks
// (ARIA 1.2 combobox: a text box that filters a list, arrows / Home / End /
// Enter / Escape, the list read out through aria-activedescendant):
//
//   _pickDevice(select, opts)    a device (or a rule target) <select>
//   _pickTemplate(select, opts)  a template <select>: the kind it is for
//                                first, then search and vendor
//   _pickPlain(select, opts)     any long <select> made searchable
//   _pickFormula(input, opts)    a formula: `device.field` completed as
//                                typed, a name the gateway lacks said with
//                                the nearest one it has
//   _tplPanel(host, opts)        the device wizard's template list (always
//                                open; the same engine)
//
// A <select> keeps living under the picker (out of sight and out of the tab
// order): the code that reads it and the scripts that drive it see the same
// element, and a choice is a native `change` on it.

Object.assign(JanitzaMonitor.prototype, {

    // ── what is offered: devices, installations, fields, templates ────────
    async _pickDevicesData() {
        const gen = this._writeGen || 0;
        if (this._pickData && this._pickData.gen === gen) return this._pickData;
        if (this._pickDataP && this._pickDataP.gen === gen) return this._pickDataP.p;
        const stale = !!this._pickData;               // a write since: ask again
        const p = (async () => {
            const [devices, eps] = await Promise.all([
                this._fetchDevices(stale).catch(() => []),
                fetch('/api/endpoints').then(r => r.json()).then(d => d.endpoints || (Array.isArray(d) ? d : []))
                    .catch(() => []),
            ]);
            const data = {
                gen, devices: devices || [], endpoints: eps || [],
                byId: new Map((devices || []).map(d => [d.id, d])),
                epById: new Map((eps || []).map(e => [e.id, e])),
            };
            this._pickData = data;
            return data;
        })().finally(() => { if (this._pickDataP?.p === p) this._pickDataP = null; });
        this._pickDataP = { gen, p };
        return p;
    },

    // the names a formula may read on a device or an installation — the
    // same set a rule's inputs are judged against (GET /api/rule-fields).
    // [{name, unit}] · null (cannot tell yet) · false (no such id)
    async _pickFields(id) {
        if (!id) return false;
        const gen = this._writeGen || 0;
        const cache = this._pickFieldCache || (this._pickFieldCache = new Map());
        const hit = cache.get(id);
        if (hit && hit.gen === gen && Date.now() - hit.at < 30000) return hit.p;
        const p = fetch(`/api/rule-fields?device=${encodeURIComponent(id)}`)
            .then(r => (r.status === 404 ? false : r.ok ? r.json().then(d => d.fields) : null))
            .catch(() => null);
        cache.set(id, { gen, at: Date.now(), p });
        return p;
    },

    async _pickTemplatesData() {
        const gen = this._writeGen || 0;
        if (this._pickTpl && this._pickTpl.gen === gen) return this._pickTpl.p;
        const p = fetch('/api/device-templates').then(r => r.json()).then(d => d.templates || []).catch(() => []);
        this._pickTpl = { gen, p };
        return p;
    },

    // a device as the lists show it: its kind, name, id, where it belongs
    // and whether it is reading
    _pickDevItem(id, data) {
        const t = (k, d, p) => this.t(k, d, p);
        const d = data.byId.get(id);
        if (!d) {
            const ep = data.epById.get(id);
            if (!ep) return null;
            return { icon: 'bi-diagram-3', label: ep.name || id, chip: id,
                     sub: t('pick.installation', 'Installation'), group: '',
                     search: `${ep.name || ''} ${id} ${t('pick.installation', 'Installation')} instalatie installation` };
        }
        const ep = d.endpoint_id ? data.epById.get(d.endpoint_id) : null;
        const parent = d.component_of ? data.byId.get(d.component_of) : null;
        const where = ep ? (ep.name || d.endpoint_id)
            : parent ? t('pick.partOf', 'part of {name}', { name: parent.name || parent.id }) : '';
        const state = this._devState(d);
        return {
            kind: d.kind || 'other', label: d.name || d.id, chip: d.id, state,
            sub: [this._kindLabel(d.kind), where].filter(Boolean).join(' · '),
            group: ep ? (ep.name || d.endpoint_id) : parent ? (parent.name || parent.id) : '',
            dim: state === 'disabled',
            search: `${d.name || ''} ${d.id} ${d.template || ''} ${where} ${(this._KIND_WORDS || {})[d.kind] || ''} ${this._kindLabel(d.kind)}`,
        };
    },

    _pickStateWord(s) {
        return ({ online: this.t('pick.online', 'reading'), offline: this.t('pick.offline', 'not reading'),
                  disabled: this.t('pick.disabled', 'disabled') })[s] || '';
    },

    // ── the engine ────────────────────────────────────────────────────────
    // c = {id, input, pop, list, opts, items, shown, active, open, ...}
    // The list is built once per set of items (one node per item); a
    // keystroke only hides and shows them — at hundreds of devices typing
    // stays immediate.
    _cbxNew(input, opts) {
        this._cbxN = (this._cbxN || 0) + 1;
        const id = `cbx${this._cbxN}`;
        const pop = document.createElement('div');
        pop.className = 'cbx-pop' + (opts.inline ? ' cbx-inline' : '');
        pop.hidden = !opts.inline;
        pop.innerHTML = `<div class="cbx-facets" hidden></div>
            <ul class="cbx-list" role="listbox" id="${id}-list"></ul>
            <div class="cbx-empty field-hint" hidden></div>`;
        const live = document.createElement('span');
        live.className = 'sr-only';
        live.setAttribute('aria-live', 'polite');
        const c = { id, input, pop, live, list: pop.querySelector('.cbx-list'), opts,
                    items: [], shown: [], active: -1, open: !!opts.inline, facet: new Set(), more: false, seq: 0, dymSeq: 0 };
        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', opts.inline ? 'true' : 'false');
        input.setAttribute('aria-controls', `${id}-list`);
        input.setAttribute('autocomplete', 'off');
        input.spellcheck = false;
        const lbl = opts.listLabel || input.getAttribute('aria-label')
            || (input.id && document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.textContent.trim()) || '';
        if (lbl) c.list.setAttribute('aria-label', lbl);
        // a click in the list never takes the focus off the text box
        pop.addEventListener('mousedown', e => { if (!e.target.closest('.cbx-facet')) e.preventDefault(); });
        pop.addEventListener('click', e => {
            const chip = e.target.closest('.cbx-facet');
            if (chip) { this._cbxToggleFacet(c, chip.dataset.v); return; }
            const li = e.target.closest('[role="option"]');
            if (!li || li.getAttribute('aria-disabled') === 'true') return;
            const i = c.shown.findIndex(it => it._li === li);
            if (i >= 0) this._cbxPick(c, i);
        });
        // Escape on a chip closes the list, not the dialog under it
        pop.addEventListener('keydown', e => {
            if (e.key !== 'Escape' || e.target === input) return;
            e.preventDefault(); e.stopPropagation();
            this._cbxCloseList(c);
            input.focus();
        });
        input.addEventListener('keydown', e => this._cbxKey(c, e));
        return c;
    },

    // one node per item (and the "show more" line), in groups; built when
    // the items change, never per keystroke
    _cbxBuild(c) {
        const t = (k, d, p) => this.t(k, d, p);
        const esc = v => this._esc(v);
        const groups = [];
        const byName = new Map();
        const moreName = c.opts.moreGroup || t('pick.other', 'Other');
        c.items.forEach(it => {
            const g = it.more ? `\u0000${moreName}` : (it.group || '');
            if (!byName.has(g)) { const grp = { name: g, label: it.more ? moreName : (it.group || ''), items: [] }; byName.set(g, grp); groups.push(grp); }
            byName.get(g).items.push(it);
        });
        // the folded section always last
        groups.sort((a, b) => a.name.startsWith('\u0000') - b.name.startsWith('\u0000'));
        let html = '', n = 0;
        const order = [];
        groups.forEach((g, gi) => {
            html += `<li role="presentation" class="cbx-grp" data-g="${gi}"><ul role="group" ${g.label
                ? `aria-labelledby="${c.id}-g${gi}"` : `aria-label="${esc(c.opts.mainGroup || t('pick.main', 'Choices'))}"`}>`
                + (g.label ? `<li role="presentation" id="${c.id}-g${gi}" class="cbx-grp-label">${esc(g.label)}</li>` : '');
            g.items.forEach(it => {
                const i = n++;
                order.push(it);
                it._text = this._fold(`${it.label} ${it.chip || ''} ${it.sub || ''} ${it.group || ''} ${it.search || ''}`);
                const ico = it.kind ? `<span class="cbx-o-ico">${this._kindIconHtml(it.kind)}</span>`
                    : it.icon ? `<span class="cbx-o-ico"><i aria-hidden="true" class="bi ${esc(it.icon)}"></i></span>` : '<span class="cbx-o-ico"></span>';
                const st = it.state ? `<span class="cbx-o-st st-${esc(it.state)}" title="${esc(this._pickStateWord(it.state))}"><span class="sr-only">${esc(this._pickStateWord(it.state))}</span></span>` : '';
                const attrs = Object.entries(it.attrs || {}).map(([k, v]) => ` ${k}="${esc(v)}"`).join('');
                const warn = it.warn ? ` <i aria-hidden="true" class="bi bi-exclamation-triangle tpl-note-flag" title="${esc(it.warn)}"></i><span class="sr-only">${esc(it.warn)}</span>` : '';
                const aside = it.aside ? `<span class="cbx-o-aside">${esc(it.aside)}</span>` : '';
                html += `<li role="option" id="${c.id}-o${i}" class="cbx-opt${c.opts.optClass ? ` ${c.opts.optClass}` : ''}${it.dim ? ' cbx-dim' : ''}" aria-selected="false"${it.disabled ? ' aria-disabled="true"' : ''}${attrs}>
                    ${ico}<span class="cbx-o-main"><span class="cbx-o-label">${esc(it.label)}</span>${warn}${it.chip && it.chip !== it.label ? ` <span class="cbx-o-chip">${esc(it.chip)}</span>` : ''}${it.sub ? `<span class="cbx-o-sub">${esc(it.sub)}</span>` : ''}</span>${aside}${st}<i aria-hidden="true" class="bi bi-check2 cbx-o-tick"></i></li>`;
            });
            html += '</ul></li>';
        });
        html += `<li role="presentation" class="cbx-grp cbx-grp-more" hidden><ul role="group" aria-label="${esc(moreName)}"><li role="option" id="${c.id}-more" class="cbx-opt cbx-more" aria-selected="false"><i aria-hidden="true" class="bi bi-chevron-down"></i> <span class="cbx-more-label"></span></li></ul></li>`;
        c.list.innerHTML = html;
        order.forEach((it, i) => { it._li = document.getElementById(`${c.id}-o${i}`); });
        c.order = order;
        c.groupEls = [...c.list.querySelectorAll(':scope > .cbx-grp[data-g]')];
        c.moreEl = document.getElementById(`${c.id}-more`);
        c.moreItem = { pseudo: 'more', _li: c.moreEl };
        c.builtFor = c.items;
        c.curEl = null;
    },

    // what the query and the chips leave: shown, the rest hidden; the
    // folded section behind one line unless searched or opened
    _cbxApply(c) {
        const t = (k, d, p) => this.t(k, d, p);
        const words = this._findWords(c.q || '');
        const hit = it => (!c.facet.size || c.facet.has(it.facet)) && words.every(w => this._findWordHit(it._text, w));
        const main = [], more = [];
        c.order.forEach(it => { if (hit(it)) (it.more ? more : main).push(it); });
        const unfold = c.more || words.length > 0 || !main.length;
        const vis = new Set(unfold ? main.concat(more) : main);
        c.order.forEach(it => { it._li.hidden = !vis.has(it); });
        c.groupEls.forEach(g => { g.hidden = !g.querySelector('[role="option"]:not([hidden])'); });
        const folded = !unfold && more.length;
        c.moreEl.closest('.cbx-grp').hidden = !folded;
        if (folded) c.moreEl.querySelector('.cbx-more-label').textContent = c.opts.moreLabel
            ? c.opts.moreLabel(more.length) : t('pick.showMore', 'Show {n} more…', { n: more.length });
        c.shown = c.order.filter(it => vis.has(it)).concat(folded ? [c.moreItem] : []);
        // the stored value: ticked
        const cur = c.opts.current ? c.opts.current() : null;
        const curIt = cur != null ? c.order.find(it => it.value === cur) : null;
        if (c.curEl !== (curIt && curIt._li)) {
            if (c.curEl) c.curEl.classList.remove('cbx-cur', 'selected');
            c.curEl = curIt ? curIt._li : null;
            if (c.curEl) c.curEl.classList.add('cbx-cur', 'selected');
        }
        const empty = c.pop.querySelector('.cbx-empty');
        empty.hidden = c.shown.length > 0;
        empty.textContent = c.shown.length ? '' : (c.opts.emptyText || t('pick.none', 'Nothing matches.'));
        this._cbxFacets(c);
        // the highlighted row: kept while walking the list; a list just
        // opened starts on the stored value and nothing else; typing starts
        // on the first match
        let i = c.activeValue != null ? c.shown.findIndex(it => it.value === c.activeValue) : -1;
        if (i < 0) i = words.length ? this._cbxNext(c, -1, 1) : (curIt ? c.shown.indexOf(curIt) : -1);
        this._cbxSetActive(c, i, false);
        if (words.length) c.list.scrollTop = 0;
        clearTimeout(c._liveT);
        c._liveT = setTimeout(() => {
            const n = c.shown.filter(x => !x.pseudo).length;
            c.live.textContent = c.open ? this.tn('pick.results', n, '{n} results') : '';
        }, 350);
    },

    _cbxRender(c) {
        if (c.builtFor !== c.items || !c.order) this._cbxBuild(c);
        this._cbxApply(c);
    },

    // the vendor chips: while nothing is typed (a typed name finds its
    // vendor already), the most common few, the rest one click away
    _cbxFacets(c) {
        const box = c.pop.querySelector('.cbx-facets');
        const f = c.opts.facet;
        const counts = new Map();
        if (f) c.items.forEach(it => { if (it.facet) counts.set(it.facet, (counts.get(it.facet) || 0) + 1); });
        if (!f || counts.size < 2 || ((c.q || '').trim() && !c.facet.size)) { box.hidden = true; return; }
        box.hidden = false;
        const all = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a) || a.localeCompare(b));
        const HEAD = 6;
        const shown = c.facetsAll ? all : all.filter((v, i) => i < HEAD || c.facet.has(v));
        const key = `${shown.join('\u0001')}|${[...c.facet].join('\u0001')}`;
        if (c._facetKey === key) return;
        c._facetKey = key;
        const chip = v => `<button type="button" class="find-chip cbx-facet" data-v="${this._esc(v)}" aria-pressed="${c.facet.has(v)}">${this._esc(v)} <span class="find-chip-n">${counts.get(v)}</span></button>`;
        box.innerHTML = `<span class="cbx-facets-label">${this._esc(f.label)}</span>` + shown.map(chip).join('')
            + (shown.length < all.length ? `<button type="button" class="btn btn-ghost btn-sm cbx-facet cbx-facet-more" data-v="">${this._esc(this.tn('find.more', all.length - shown.length, '{n} more'))}</button>` : '');
    },

    _cbxToggleFacet(c, v) {
        if (!v) c.facetsAll = true;
        else if (c.facet.has(v)) c.facet.delete(v);
        else c.facet.add(v);
        this._cbxApply(c);
    },

    _cbxNext(c, from, dir) {
        const n = c.shown.length;
        for (let i = from + dir; i >= 0 && i < n; i += dir) {
            if (!c.shown[i].disabled) return i;
        }
        return from >= 0 && from < n ? from : -1;
    },

    // the highlighted row is the selected one for a screen reader (APG); the
    // stored value keeps its tick
    _cbxSetActive(c, i, scroll = true) {
        if (c.activeEl) { c.activeEl.classList.remove('cbx-active'); c.activeEl.setAttribute('aria-selected', 'false'); }
        c.active = i;
        const it = c.shown[i];
        c.activeValue = it && !it.pseudo ? it.value : null;
        c.activeEl = it ? it._li : null;
        if (!c.activeEl) { c.input.removeAttribute('aria-activedescendant'); return; }
        c.activeEl.classList.add('cbx-active');
        c.activeEl.setAttribute('aria-selected', 'true');
        if (c.open) c.input.setAttribute('aria-activedescendant', c.activeEl.id);
        if (scroll) c.activeEl.scrollIntoView({ block: 'nearest' });
    },

    _cbxOpenList(c) {
        if (c.opts.inline) return;
        const fresh = !c.open;
        if (fresh) {
            c.open = true;
            c.pop.hidden = false;
            if (c.input.getAttribute('role') === 'combobox') c.input.setAttribute('aria-expanded', 'true');
            c._onMove = () => this._cbxPlace(c);
            window.addEventListener('scroll', c._onMove, true);
            window.addEventListener('resize', c._onMove);
        }
        this._cbxRender(c);
        if (fresh) {
            c._placed = '';
            this._cbxPlace(c);
            cancelAnimationFrame(c._raf);
            c._raf = requestAnimationFrame(() => this._cbxFollow(c));
        }
        if (c.activeEl) { c.input.setAttribute('aria-activedescendant', c.activeEl.id); if (fresh) c.activeEl.scrollIntoView({ block: 'nearest' }); }
    },

    // closing forgets the highlight: a list opened again starts on the
    // stored value, never on a row left from an abandoned search
    _cbxCloseList(c) {
        c.activeValue = null;
        if (c.opts.inline || !c.open) return;
        c.open = false;
        if (c.activeEl) { c.activeEl.classList.remove('cbx-active'); c.activeEl.setAttribute('aria-selected', 'false'); }
        c.activeEl = null;
        c.active = -1;
        c.pop.hidden = true;
        if (c.input.getAttribute('role') === 'combobox') c.input.setAttribute('aria-expanded', 'false');
        c.input.removeAttribute('aria-activedescendant');
        c.live.textContent = '';
        window.removeEventListener('scroll', c._onMove, true);
        window.removeEventListener('resize', c._onMove);
        cancelAnimationFrame(c._raf);
    },

    // under the text box when it fits there, above it only when it does not
    // and the room is there instead; fixed to the (visual) viewport, measured
    // from where "fixed" 0,0 really is (a dialog's opening transform makes it
    // its own box for a moment)
    _cbxPlace(c) {
        if (!c.open) return;
        const a = (c.anchor || c.input).getBoundingClientRect();
        const vv = window.visualViewport;
        const vw = document.documentElement.clientWidth;
        const vTop = vv ? vv.offsetTop : 0, vh = vv ? vv.height : window.innerHeight;
        const w = Math.min(Math.max(a.width, c.opts.minWidth || 280), vw - 16);
        const below = vTop + vh - a.bottom - 6, above = a.top - vTop - 6;
        const facets = c.pop.querySelector('.cbx-facets');
        const empty = c.pop.querySelector('.cbx-empty');
        // the list's own height (rows, chips, the "nothing" line, its border)
        const natural = Math.min(c.opts.maxHeight || 340, c.list.scrollHeight + 2
            + (facets && !facets.hidden ? facets.offsetHeight : 0) + (empty && !empty.hidden ? empty.offsetHeight : 0));
        const up = natural > below && above > below;
        const maxH = Math.max(120, Math.min(c.opts.maxHeight || 340, (up ? above : below) - 4));
        const s = c.pop.style;
        s.position = 'fixed';
        s.width = `${w}px`;
        s.maxHeight = `${maxH}px`;
        s.top = '0px';
        s.left = '0px';
        const o = c.pop.getBoundingClientRect();
        const h = c.pop.offsetHeight;
        const wantTop = up ? a.top - h - 4 : a.bottom + 4;
        const wantLeft = Math.max(8, Math.min(a.left, vw - w - 8));
        s.top = `${wantTop - o.top}px`;
        s.left = `${wantLeft - o.left}px`;
        c._want = { top: wantTop, left: wantLeft };
    },

    // while open, the list follows its box and its own height (a dialog
    // that animates, scrolls or grows; a list a keystroke shortened) — one
    // measurement a frame, only while shown
    _cbxFollow(c) {
        if (!c.open || c.opts.inline) return;
        const a = (c.anchor || c.input).getBoundingClientRect();
        const key = `${Math.round(a.left)},${Math.round(a.top)},${Math.round(a.width)},${window.innerHeight},${c.list.scrollHeight}`;
        // the list not where it was put: what "fixed" is relative to changed
        // (a dialog's opening animation ended)
        const r = c.pop.getBoundingClientRect();
        const off = c._want && (Math.abs(r.top - c._want.top) > 1 || Math.abs(r.left - c._want.left) > 1);
        if (key !== c._placed || off) { c._placed = key; this._cbxPlace(c); }
        c._raf = requestAnimationFrame(() => this._cbxFollow(c));
    },

    _cbxKey(c, e) {
        if (e.isComposing) return;
        const k = e.key;
        const stop = () => { e.preventDefault(); e.stopPropagation(); };
        if (c.opts.onKey && c.opts.onKey(c, e) === false) return;
        if (k === 'ArrowDown' || k === 'ArrowUp') {
            if (c.opts.formula && !c.open) return;
            stop();
            if (e.altKey) { if (k === 'ArrowDown') this._cbxOpenList(c); else this._cbxCloseList(c); return; }
            // a closed list only opens: the value changes on Enter, never on
            // an arrow
            if (!c.open) { this._cbxOpenList(c); return; }
            this._cbxSetActive(c, c.active < 0 && k === 'ArrowDown' ? this._cbxNext(c, -1, 1)
                : this._cbxNext(c, c.active, k === 'ArrowDown' ? 1 : -1));
            return;
        }
        if ((k === 'PageDown' || k === 'PageUp') && c.open) {
            stop();
            let i = c.active;
            for (let s = 0; s < 8; s++) i = this._cbxNext(c, i, k === 'PageDown' ? 1 : -1);
            this._cbxSetActive(c, i);
            return;
        }
        // in a formula Home and End move the caret, as in any text
        if ((k === 'Home' || k === 'End') && c.open && c.shown.length && !c.opts.formula) {
            stop();
            this._cbxSetActive(c, k === 'Home' ? this._cbxNext(c, -1, 1) : this._cbxNext(c, c.shown.length, -1));
            return;
        }
        if (k === 'Enter' && c.open && c.active >= 0) {
            stop();
            this._cbxPick(c, c.active);
            return;
        }
        if (k === 'Escape') {
            if (c.open && !c.opts.inline) { stop(); this._cbxCloseList(c); if (c.opts.onEscape) c.opts.onEscape(c); return; }
            if (c.opts.onEscape && c.opts.onEscape(c) === true) stop();
            return;
        }
        if (k === 'Tab' && c.open && !c.opts.inline) {
            // the chips sit in the list's box: Tab may walk into them
            if (!e.shiftKey && !c.pop.querySelector('.cbx-facets:not([hidden]) .cbx-facet')) this._cbxCloseList(c);
        }
    },

    _cbxPick(c, i) {
        const it = c.shown[i];
        if (!it) return;
        if (it.pseudo === 'more') {
            c.more = true;
            const n = c.shown.length - 1;
            this._cbxApply(c);
            this._cbxSetActive(c, this._cbxNext(c, n - 1, 1));
            return;
        }
        c.opts.onPick(it, c);
    },

    // ── a <select> as a picker ────────────────────────────────────────────
    // opts: {meta(value, option) → item fields, extra() → [items not in the
    //        select, shown folded], facet: {label}, moreLabel(n), moreGroup,
    //        boxLabel(item) → what the closed box says}
    _cbxSelect(sel, opts = {}) {
        if (!sel) return null;
        if (sel._cbx) { sel._cbx.opts = { ...sel._cbx.opts, ...opts }; sel._cbx.items = null; this._cbxSync(sel._cbx); return sel._cbx; }
        // the classes that place the control go on the box around it (the
        // device workspace hides its .view-device-sel; a toolbar sizes its
        // .diag-filter), the ones that look like a field on the text box
        const LAYOUT = /^(view-device-sel|reg-device-sel|diag-filter)$/;
        const wrap = document.createElement('div');
        wrap.className = ['cbx', ...[...sel.classList].filter(x => LAYOUT.test(x))].join(' ');
        sel.parentNode.insertBefore(wrap, sel);
        const input = document.createElement('input');
        input.type = 'text';
        input.id = `${sel.id || 'sel'}-pick`;
        input.className = `${[...sel.classList].filter(x => !LAYOUT.test(x)).join(' ')} cbx-input`;
        if (sel.title) input.title = sel.title;
        const lab = sel.id && document.querySelector(`label[for="${CSS.escape(sel.id)}"]`);
        if (lab) { lab.htmlFor = input.id; lab.dataset.forSelect = sel.id; }
        else if (sel.getAttribute('aria-label')) input.setAttribute('aria-label', sel.getAttribute('aria-label'));
        const ico = document.createElement('span');
        ico.className = 'cbx-ico';
        ico.setAttribute('aria-hidden', 'true');
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cbx-btn';
        btn.tabIndex = -1;
        btn.setAttribute('aria-hidden', 'true');
        btn.innerHTML = '<i class="bi bi-chevron-down"></i>';
        wrap.append(ico, input, btn, sel);
        sel.classList.add('cbx-native');
        sel.setAttribute('aria-hidden', 'true');
        sel.tabIndex = -1;
        const c = this._cbxNew(input, {
            ...opts,
            current: () => sel.value,
            onPick: (it, cc) => this._cbxSelectChoose(cc, it),
            onEscape: cc => { const changed = input.value !== cc.label; cc.q = ''; this._cbxSync(cc); return changed; },
        });
        c.sel = sel;
        c.wrap = wrap;
        c.anchor = wrap;
        c.items = null;
        sel._cbx = c;
        wrap.append(c.pop, c.live);
        // the hidden select never keeps the focus: whatever aims at it (an
        // error placed on a field, a script) lands on the box a person sees
        sel.addEventListener('focus', () => { if (!input.disabled) input.focus(); });
        const openFresh = () => {
            c.q = '';
            c.activeValue = null;
            if (!c.items) c.items = this._cbxSelectItems(c);
            this._cbxSync(c);
            this._cbxOpenList(c);
        };
        btn.addEventListener('mousedown', e => e.preventDefault());
        btn.addEventListener('click', () => {
            if (input.disabled) return;
            input.focus();
            if (c.open) this._cbxCloseList(c); else openFresh();
        });
        input.addEventListener('focus', () => { input.select(); });
        input.addEventListener('click', () => { if (!c.open && !input.disabled) openFresh(); });
        input.addEventListener('input', () => {
            if (!c.items) c.items = this._cbxSelectItems(c);
            c.q = input.value;
            c.activeValue = null;
            this._cbxOpenList(c);
        });
        input.addEventListener('keydown', e => {
            // opened from the keyboard: the whole list, the stored value active
            if (!c.open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { c.q = ''; c.activeValue = null; if (!c.items) c.items = this._cbxSelectItems(c); this._cbxSync(c); }
        }, true);
        wrap.addEventListener('focusout', e => {
            if (wrap.contains(e.relatedTarget)) return;
            this._cbxCloseList(c);
            c.q = '';
            this._cbxSync(c);
        });
        // the code that fills or sets the select: the box follows it
        for (const prop of ['value', 'selectedIndex']) {
            const d = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, prop);
            Object.defineProperty(sel, prop, {
                configurable: true,
                get() { return d.get.call(this); },
                set: v => { d.set.call(sel, v); this._cbxSync(c); },
            });
        }
        sel.addEventListener('change', () => this._cbxSync(c));
        c.mo = new MutationObserver(muts => {
            if (muts.some(m => m.type === 'childList' || m.attributeName === 'selected' || m.attributeName === 'label')) {
                c.items = null;
                this._cbxPrebuild(c);
            }
            this._cbxSync(c);
        });
        c.mo.observe(sel, { childList: true, subtree: true, attributes: true,
                            attributeFilter: ['style', 'hidden', 'disabled', 'selected', 'label', 'aria-invalid', 'aria-describedby'] });
        this._cbxSync(c);
        this._cbxPrebuild(c);
        return c;
    },

    // the list made while the page is idle, so opening it only shows it
    _cbxPrebuild(c) {
        if (c._prebuildT) return;
        const run = () => {
            c._prebuildT = null;
            if (c.open || c.items || !c.sel.isConnected) return;
            c.items = this._cbxSelectItems(c);
            this._cbxBuild(c);
        };
        c._prebuildT = window.requestIdleCallback ? requestIdleCallback(run, { timeout: 400 }) : setTimeout(run, 120);
    },

    _cbxSelectItems(c) {
        const out = [];
        const meta = c.opts.meta;
        [...c.sel.options].forEach(o => {
            const g = o.parentElement && o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : '';
            const base = { value: o.value, label: o.textContent.trim(), group: g, disabled: o.disabled };
            out.push({ ...base, ...(meta ? meta(o.value, o, base) || {} : {}) });
        });
        if (c.opts.extra) {
            const have = new Set(out.map(x => x.value));
            c.opts.extra().filter(x => !have.has(x.value)).forEach(x => out.push({ ...x, more: true }));
        }
        if (c.opts.order) c.opts.order(out);
        return out;
    },

    _cbxSelectChoose(c, it) {
        const sel = c.sel;
        if (![...sel.options].some(o => o.value === it.value)) {
            const o = document.createElement('option');
            o.value = it.value;
            o.textContent = it.label;
            sel.appendChild(o);
        }
        const changed = sel.value !== it.value;
        c.q = '';
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, it.value);
        this._cbxCloseList(c);
        this._cbxSync(c);
        c.input.focus();
        if (changed) {
            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
    },

    // the box shows what the select holds (its name and, for a device, its
    // id), is shown and enabled as it is, and carries its error
    _cbxSync(c) {
        const sel = c.sel;
        const o = sel.selectedOptions && sel.selectedOptions[0];
        const base = o ? { value: o.value, label: o.textContent.trim() } : null;
        const it = o ? { ...base, ...(c.opts.meta ? c.opts.meta(o.value, o, base) || {} : {}) } : null;
        c.label = it && it.value !== '' ? (c.opts.boxLabel ? c.opts.boxLabel(it) : (it.label || '')) : '';
        if (document.activeElement !== c.input || !c.open) c.input.value = c.label;
        const empty = [...sel.options].find(x => x.value === '');
        c.input.placeholder = empty ? empty.textContent.trim() : (c.opts.placeholder || this.t('pick.choose', 'Choose…'));
        const ico = c.wrap.querySelector('.cbx-ico');
        const icoHtml = it && it.value !== '' && it.kind
            ? `<i class="bi ${this._KIND_ICON[it.kind] || this._KIND_ICON.other}"></i>`
            : it && it.value !== '' && it.icon ? `<i class="bi ${this._esc(it.icon)}"></i>` : '';
        if (ico.innerHTML !== icoHtml) ico.innerHTML = icoHtml;
        c.wrap.classList.toggle('cbx-has-ico', !!icoHtml);
        for (const a of ['aria-invalid', 'aria-describedby']) {
            if (sel.hasAttribute(a)) c.input.setAttribute(a, sel.getAttribute(a));
            else c.input.removeAttribute(a);
        }
        const hidden = sel.hidden || sel.style.display === 'none';
        c.wrap.hidden = hidden;
        c.input.disabled = sel.disabled;
        if (hidden || sel.disabled) this._cbxCloseList(c);
        else if (c.open) { if (!c.items) c.items = this._cbxSelectItems(c); this._cbxRender(c); }
    },

    // the visible box of a field, when a picker stands in for it
    _pickVisible(el) {
        return el && el._cbx && el._cbx.input ? el._cbx.input : el;
    },

    // ── the three kinds of picker ─────────────────────────────────────────
    // a device <select> (values are device ids, or a rule target's
    // "d:<id>" / "g:<installation>/<group>"); grouped by installation
    async _pickDevice(sel, opts = {}) {
        if (typeof sel === 'string') sel = document.getElementById(sel);
        if (!sel) return null;
        const data = await this._pickDevicesData();
        const t = (k, d, p) => this.t(k, d, p);
        const meta = (v, o, base) => {
            if (v === '') return { icon: 'bi-dash', group: '' };
            let id = v;
            if (/^d:/.test(v)) id = v.slice(2);
            else if (/^g:/.test(v)) {
                const [ep, grp] = v.slice(2).split('/');
                const e = data.epById.get(ep);
                return { icon: 'bi-diagram-3', chip: `${ep} / ${grp}`, label: base.label,
                         sub: t('pick.wholeGroup', 'every unit of the group'), group: e ? (e.name || ep) : ep,
                         search: `${e?.name || ''} ${ep} ${grp} ${t('pick.wholeGroup', 'every unit of the group')}` };
            }
            const it = this._pickDevItem(id, data);
            if (!it) return { icon: 'bi-question-circle', sub: t('pick.notHere', 'not on this gateway') };
            // the device's name, its id as the chip; a note the select carries
            // ("— not offered now") is kept
            return { ...it, label: / — /.test(base.label || '') ? base.label : it.label, group: it.group };
        };
        const c = this._cbxSelect(sel, {
            meta,
            // a write target is never ambiguous: its id beside its name
            boxLabel: it => (it.kind && it.chip && it.chip !== it.label && !/ — /.test(it.label) ? `${it.label} · ${it.chip}` : it.label),
            mainGroup: t('pick.devices', 'Devices'),
            minWidth: 300,
            order: items => {
                // standalone devices first, then each installation's own
                const rank = it => (it.value === '' ? 0 : it.group ? 2 : 1);
                items.sort((a, b) => rank(a) - rank(b) || (a.group || '').localeCompare(b.group || ''));
            },
            ...opts,
        });
        return c;
    },

    // a template <select>: what fits the kind the place is for first, the
    // others folded under "Other kinds", a vendor chip row
    async _pickTemplate(sel, opts = {}) {
        if (typeof sel === 'string') sel = document.getElementById(sel);
        if (!sel) return null;
        const tpls = await this._pickTemplatesData();
        const byId = new Map(tpls.map(x => [x.id, x]));
        const t = (k, d, p) => this.t(k, d, p);
        const kind = () => (typeof opts.kind === 'function' ? opts.kind() : opts.kind) || '';
        const transport = () => (typeof opts.transport === 'function' ? opts.transport() : opts.transport) || '';
        const item = (x, base = {}) => ({
            value: x.id, label: base.label && base.label !== x.id ? base.label.replace(/ · (http|modbus|mqtt)$/i, '') : (x.name || x.id),
            chip: x.id, kind: x.device_kind || x.kind || 'other', facet: x.vendor || t('pick.noVendor', 'No vendor'),
            sub: [x.vendor, x.model, ({ modbus: 'Modbus', http: 'HTTP', mqtt: 'MQTT' })[x.transport] || x.transport,
                  x.builtin ? t('devices.builtin', 'built-in') : t('pick.mine', 'mine')].filter(Boolean).join(' · '),
            search: `${x.vendor || ''} ${x.model || ''} ${x.id} ${(this._KIND_WORDS || {})[x.device_kind] || ''} ${x.transport || ''}`,
        });
        const c = this._cbxSelect(sel, {
            meta: (v, o, base) => {
                if (v === '') return { icon: 'bi-dash' };
                const x = byId.get(v);
                if (!x) return { icon: 'bi-file-earmark' };
                const k = kind();
                const it = item(x, base);
                return { ...it, group: k && (x.device_kind === k || x.kind === k)
                    ? t('pick.recommended', 'For {kind}', { kind: this._kindLabel(k) }) : base.group };
            },
            // the other kinds: there to be found, not in the way
            extra: () => {
                const tr = transport();
                return tpls.filter(x => !tr || (x.transport || 'modbus') === tr).map(x => item(x));
            },
            moreGroup: t('pick.otherKinds', 'Other kinds'),
            moreLabel: n => this.tn('pick.showOtherKinds', n, 'Show {n} templates of other kinds…'),
            facet: { label: t('find.facet.vendor', 'Vendor') },
            minWidth: 360,
            ...opts,
        });
        return c;
    },

    // any long <select>: searchable, grouped by its optgroups
    _pickPlain(sel, opts = {}) {
        if (typeof sel === 'string') sel = document.getElementById(sel);
        return sel ? this._cbxSelect(sel, opts) : null;
    },

    // ── a formula: `device.field` completed as typed ──────────────────────
    // opts: {own: the device a bare name belongs to (a calculated field)}
    async _pickFormula(input, opts = {}) {
        if (typeof input === 'string') input = document.getElementById(input);
        if (!input || input._cbx) return input?._cbx || null;
        const data = await this._pickDevicesData();
        const t = (k, d, p) => this.t(k, d, p);
        const wrap = document.createElement('div');
        wrap.className = 'cbx cbx-formula';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        const dym = document.createElement('div');
        dym.className = 'cbx-dym field-hint';
        dym.hidden = true;
        dym.setAttribute('aria-live', 'polite');
        dym.id = `${input.id || 'f'}-dym`;
        const c = this._cbxNew(input, {
            ...opts, formula: true, minWidth: 300,
            listLabel: t('pick.suggestions', 'Suggestions'),
            onPick: (it, cc) => this._fxInsert(cc, it),
        });
        // a <textarea> stays a text box (it cannot be a combobox): it still
        // names its list and its active suggestion
        if (input.tagName === 'TEXTAREA') { input.removeAttribute('role'); input.removeAttribute('aria-expanded'); }
        c.anchor = input;
        c.data = data;
        input._cbx = c;
        wrap.append(c.pop, c.live, dym);
        c.dym = dym;
        input.setAttribute('aria-describedby', [input.getAttribute('aria-describedby'), dym.id].filter(Boolean).join(' '));
        const run = (force = false) => this._fxSuggest(c, force);
        input.addEventListener('input', () => { run(); this._fxCheckSoon(c); });
        input.addEventListener('click', () => { if (c.open) run(); });
        input.addEventListener('keydown', e => {
            if (e.key === ' ' && e.ctrlKey) { e.preventDefault(); run(true); }
        });
        input.addEventListener('keyup', e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { if (c.open) run(); } });
        // the "did you mean" line follows the typing, never the leaving: a
        // line appearing on blur would move the button being clicked
        wrap.addEventListener('focusout', e => {
            if (wrap.contains(e.relatedTarget)) return;
            this._cbxCloseList(c);
        });
        dym.addEventListener('click', e => {
            const b = e.target.closest('[data-fx-use]');
            if (!b) return;
            const from = b.dataset.from, to = b.dataset.fxUse;
            const v = input.value;
            const at = this._fxFindRef(v, from, b.dataset.kind === 'device');
            if (at < 0) return;
            input.value = v.slice(0, at) + to + v.slice(at + from.length);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
            input.setSelectionRange(at + to.length, at + to.length);
        });
        if (input.value) this._fxCheckSoon(c, 50);
        return c;
    },

    // the name at the caret: a device (no dot yet) or a field of a device
    // (after "device."), with the part of it after the caret (an edit in the
    // middle of a name replaces the whole name); a hyphen is also a minus
    // sign — "20000-meter.p" reads meter after the number — so a token a-b
    // whose whole is no device is read from after its last hyphen
    _FX_LEAD: String.raw`(^|[^\w.\-]|(?<![\w.])\d+(?:\.\d+)?-)`,

    _fxToken(input) {
        const v = input.value, at = input.selectionStart ?? v.length;
        const before = v.slice(0, at);
        if (/["'][^"']*$/.test(before.replace(/"[^"]*"|'[^']*'/g, ''))) return null;   // inside a string
        const m = before.match(new RegExp(`${this._FX_LEAD}([A-Za-z_][\\w\\-]*)(?:\\.(\\w*))?$`));
        if (!m) return null;
        const whole = m[2], field = m[3];
        const start = at - m[0].length + m[1].length;
        const after = v.slice(at);
        const right = (field !== undefined ? after.match(/^\w*/) : after.match(/^[\w\-]*/))[0];
        return { whole, field, start, end: at, right: right.length, hasDot: field !== undefined,
                 call: after.slice(right.length).startsWith('(') };
    },

    _fxDevKnown(c, id) {
        return c.data.byId.has(id) || c.data.epById.has(id);
    },

    async _fxSuggest(c, force) {
        const t = (k, d, p) => this.t(k, d, p);
        const input = c.input;
        const tok = this._fxToken(input);
        const ask = ++c.seq;
        if (!tok) { this._cbxCloseList(c); return; }
        let q = '', replaceFrom = tok.start, key, make;
        if (tok.hasDot) {
            let dev = tok.whole;
            if (!this._fxDevKnown(c, dev) && dev.includes('-')) {
                const tail = dev.slice(dev.lastIndexOf('-') + 1);
                if (this._fxDevKnown(c, tail)) { replaceFrom += dev.length - tail.length; dev = tail; }
            }
            if (!this._fxDevKnown(c, dev)) { this._cbxCloseList(c); return; }
            const fields = await this._pickFields(dev);
            if (ask !== c.seq || document.activeElement !== input) return;
            q = tok.field || '';
            key = `f:${dev}:${(fields || []).length}`;
            make = () => (fields || []).map(f => ({ value: f.name, label: f.name, chip: f.unit || '', ins: f.name }));
            c.repl = { from: tok.end - (tok.field || '').length, to: tok.end + tok.right };
            if (!(fields || []).length) { this._cbxCloseList(c); return; }
        } else {
            let word = tok.whole;
            // a function being called ("max(") is not a device; one letter is
            // too little to guess from
            if (!force && (tok.call || word.length < 2)) { this._cbxCloseList(c); return; }
            let own = [];
            if (c.opts.own) {
                own = (await this._pickFields(c.opts.own)) || [];
                if (ask !== c.seq || document.activeElement !== input) return;
            }
            key = `d:${c.data.gen}:${own.length}`;
            make = () => {
                const items = own.map(f => ({ value: `own:${f.name}`, label: f.name, chip: f.unit || '',
                    group: t('pick.ownFields', 'This device'), ins: f.name }));
                c.data.devices.forEach(d => {
                    const it = this._pickDevItem(d.id, c.data);
                    if (it) items.push({ ...it, value: `dev:${d.id}`, ins: `${d.id}.`, group: t('pick.devicesDot', 'Devices — then a field') });
                });
                c.data.endpoints.forEach(e => items.push({ ...this._pickDevItem(e.id, c.data), value: `dev:${e.id}`,
                    ins: `${e.id}.`, group: t('pick.installationsDot', 'Installations — then a total') }));
                return items;
            };
            // a token a-b: the device after the minus, when the whole is none
            const ids = [...c.data.byId.keys(), ...c.data.epById.keys()];
            if (word.includes('-') && !ids.some(id => this._fold(id).startsWith(this._fold(word)))) {
                const tail = word.slice(word.lastIndexOf('-') + 1);
                replaceFrom += word.length - tail.length;
                word = tail;
            }
            q = word;
            c.repl = { from: replaceFrom, to: tok.end + tok.right };
        }
        if (c.itemsKey !== key) { c.items = make(); c.itemsKey = key; }
        // nothing left to complete: the name typed is the only one it can be
        const fq = this._fold(q);
        const starts = c.items.filter(it => this._fold(it.label).startsWith(fq) || this._fold((it.ins || '').replace(/\.$/, '')).startsWith(fq));
        if (!force && fq && !tok.right && starts.length === 1 && this._fold((starts[0].ins || '').replace(/\.$/, '')) === fq) { this._cbxCloseList(c); return; }
        c.q = q;
        c.activeValue = null;
        if (!this._cbxOpenOrNone(c)) this._cbxCloseList(c);
    },

    // open the list when the query leaves something to offer
    _cbxOpenOrNone(c) {
        if (c.builtFor !== c.items || !c.order) this._cbxBuild(c);
        const words = this._findWords(c.q || '');
        if (!c.order.some(it => words.every(w => this._findWordHit(it._text, w)))) return false;
        this._cbxOpenList(c);
        return true;
    },

    _fxInsert(c, it) {
        const input = c.input, v = input.value;
        const { from } = c.repl || { from: input.selectionStart };
        let { to } = c.repl || { to: input.selectionStart };
        // a device put in place of a device: its dot is the one already there
        if (it.ins.endsWith('.') && v[to] === '.') to += 1;
        input.value = v.slice(0, from) + it.ins + v.slice(to);
        const pos = from + it.ins.length;
        input.setSelectionRange(pos, pos);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
        // a device chosen: its fields next
        if (it.ins.endsWith('.')) this._fxSuggest(c, true);
        else this._cbxCloseList(c);
    },

    // where a reference named in a note sits: a device name followed by its
    // dot, a device.field as a whole name — never inside a longer name or a
    // string
    _fxFindRef(v, ref, isDevice) {
        const src = v.replace(/"[^"]*"|'[^']*'/g, m => ' '.repeat(m.length));
        const esc = ref.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const re = new RegExp(`${this._FX_LEAD}(${esc})${isDevice ? String.raw`(?=\.)` : String.raw`(?![\w])`}`);
        const m = src.match(re);
        return m ? m.index + m[1].length : -1;
    },

    _fxCheckSoon(c, ms = 500) {
        clearTimeout(c._dymT);
        c._dymT = setTimeout(() => this._fxCheck(c), ms);
    },

    // a `device.field` the gateway lacks: said under the box, with the
    // nearest name it has (the rule's own check says it again on save)
    async _fxCheck(c) {
        const t = (k, d, p) => this.t(k, d, p);
        const src = (c.input.value || '').replace(/"[^"]*"|'[^']*'/g, m => ' '.repeat(m.length));
        const refs = [...src.matchAll(new RegExp(`${this._FX_LEAD}([A-Za-z_][\\w\\-]*)\\.([A-Za-z_]\\w*)`, 'g'))].map(m => ({ dev: m[2], field: m[3] }));
        const ask = ++c.dymSeq;
        const notes = [];
        for (const r of refs) {
            let dev = r.dev;
            if (!this._fxDevKnown(c, dev)) {
                const tail = dev.includes('-') ? dev.slice(dev.lastIndexOf('-') + 1) : '';
                if (tail && this._fxDevKnown(c, tail)) dev = tail;
                else {
                    const ids = c.data.devices.map(d => d.id).concat(c.data.endpoints.map(e => e.id));
                    const near = this._fxNearest(r.dev, ids);
                    notes.push({ kind: 'device', from: r.dev, to: near, text: near
                        ? t('pick.dymDevice', 'No device "{id}" here. Did you mean {near}?', { id: r.dev, near })
                        : t('pick.noDevice', 'No device "{id}" here.', { id: r.dev }) });
                    continue;
                }
            }
            const fields = await this._pickFields(dev);
            if (ask !== c.dymSeq) return;
            if (!fields || fields.some(f => f.name === r.field)) continue;
            const near = this._fxNearest(r.field, fields.map(f => f.name));
            notes.push({ kind: 'field', from: `${dev}.${r.field}`, to: near ? `${dev}.${near}` : '', text: near
                ? t('pick.dymField', '{id} reads no "{field}". Did you mean {near}?', { id: dev, field: r.field, near })
                : t('pick.noField', '{id} reads no "{field}".', { id: dev, field: r.field }) });
        }
        if (ask !== c.dymSeq) return;
        const seen = new Set();
        const uniq = notes.filter(n => !seen.has(n.text) && seen.add(n.text)).slice(0, 3);
        c.dym.hidden = !uniq.length;
        c.dym.innerHTML = uniq.map(n => `<div class="cbx-dym-row"><i aria-hidden="true" class="bi bi-question-circle"></i> ${this._esc(n.text)}${n.to
            ? ` <button type="button" class="btn btn-ghost btn-sm cbx-dym-use" data-fx-use="${this._esc(n.to)}" data-from="${this._esc(n.from)}" data-kind="${n.kind}">${this._esc(t('pick.use', 'Use {name}', { name: n.to }))}</button>` : ''}</div>`).join('');
    },

    // the nearest name: by edit distance, near enough to be a typo
    _fxNearest(word, names) {
        const a = this._fold(word);
        let best = '', bd = Infinity;
        for (const n of names) {
            const b = this._fold(n);
            const d = this._fxDist(a, b);
            if (d < bd) { bd = d; best = n; }
        }
        const limit = Math.max(2, Math.ceil(a.length / 3));
        return bd <= limit ? best : (best && (this._fold(best).startsWith(a) || a.startsWith(this._fold(best))) ? best : '');
    },

    _fxDist(a, b) {
        if (a === b) return 0;
        const m = a.length, n = b.length;
        if (!m || !n) return m || n;
        let prev = Array.from({ length: n + 1 }, (_, j) => j);
        for (let i = 1; i <= m; i++) {
            const cur = [i];
            for (let j = 1; j <= n; j++) {
                cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
            }
            prev = cur;
        }
        return prev[n];
    },
});
