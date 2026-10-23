/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// find — augments JanitzaMonitor.prototype
//
// Finding a device or a template among hundreds: one search box that hides
// and shows rows (a keystroke never re-renders the list), filter chips with
// counts — any value inside a filter, every filter together — a tree or a
// list, all of it kept in the address so Back, a reload and a shared link
// land on the same view.

Object.assign(JanitzaMonitor.prototype, {

    // ── kinds (the server's device_kind.py taxonomy) ──────────────────────
    _KIND_ICON: {
        meter: 'bi-speedometer2', inverter: 'bi-sun', battery: 'bi-battery-half',
        site: 'bi-hdd-stack', heat_pump: 'bi-thermometer-sun', drive: 'bi-gear-wide-connected',
        ups: 'bi-plug', charger: 'bi-ev-station', sensor: 'bi-thermometer-half', other: 'bi-box',
    },
    _KIND_ORDER: ['meter', 'inverter', 'battery', 'site', 'heat_pump', 'drive', 'ups', 'charger', 'sensor', 'other'],

    _kindLabel(k) {
        const en = { meter: 'Meter', inverter: 'Inverter', battery: 'Battery / BMS', site: 'System / GX',
                     heat_pump: 'Heat pump', drive: 'Drive (VFD)', ups: 'UPS', charger: 'EV charger',
                     sensor: 'Sensor', other: 'Other' };
        return this.t(`kind.${k || 'other'}`, en[k] || en.other);
    },

    // the kind as a small icon a sighted user scans for, and words for a
    // screen reader (the row's name follows it)
    _kindIconHtml(k) {
        const kind = this._KIND_ICON[k] ? k : 'other';
        const label = this._esc(this._kindLabel(kind));
        return `<span class="kind-ico" title="${label}"><i aria-hidden="true" class="bi ${this._KIND_ICON[kind]}"></i><span class="sr-only">${label}: </span></span>`;
    },

    // in a table cell the word is shown, the icon only decorates it
    _kindCellHtml(k) {
        const kind = this._KIND_ICON[k] ? k : 'other';
        return `<i aria-hidden="true" class="bi ${this._KIND_ICON[kind]} kind-ico-i"></i> ${this._esc(this._kindLabel(kind))}`;
    },

    _kindSourceWords(src) {
        const t = (k, d) => this.t(k, d);
        return ({
            override: t('kind.src.override', 'set by an administrator'),
            role: t('kind.src.role', 'from its group in the installation'),
            component: t('kind.src.component', 'from its parent device'),
            template: t('kind.src.template', 'from its template'),
            names: t('kind.src.names', 'from what it measures'),
        })[src] || t('kind.src.none', 'not known');
    },

    // the words a kind is found by, in both languages (the UI speaks one,
    // the person typing may think in the other)
    _KIND_WORDS: {
        meter: 'meter contor', inverter: 'inverter invertor', battery: 'battery bms baterie acumulator',
        site: 'system gx sistem site', heat_pump: 'heat pump pompa caldura', drive: 'drive vfd variator convertizor',
        ups: 'ups', charger: 'charger ev incarcator statie incarcare', sensor: 'sensor senzor',
        other: 'other altele',
    },

    // case and diacritics never matter ("hala" finds "Hală"; ş/ș, ţ/ț alike)
    _fold(s) {
        return String(s ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    },

    // ── the engine: items, a state, counts ────────────────────────────────
    // item = {id, f: {facet: [values]}, text (folded, words separated by
    // spaces)}; state = {q, sel: {facet: Set}}
    _findWords(q) {
        return this._fold(q).split(/\s+/).filter(Boolean);
    },

    // a number typed alone is a whole token ("7" finds "unit 7", not port
    // 20007); any other word is found anywhere ("inv" finds "inverter")
    _findWordHit(text, w) {
        if (!/^\d+$/.test(w)) return text.includes(w);
        return new RegExp(`(^|[^0-9])${w}([^0-9]|$)`).test(text);
    },

    _findItemMatches(st, it, except, words) {
        for (const f of Object.keys(st.sel)) {
            if (f === except) continue;
            const s = st.sel[f];
            if (s.size && !(it.f[f] || []).some(v => s.has(v))) return false;
        }
        return words.every(w => this._findWordHit(it.text, w));
    },

    _findFiltering(st) {
        return !!(this._findWords(st.q).length || Object.values(st.sel).some(s => s.size));
    },

    _findMatched(st, items) {
        const words = this._findWords(st.q);
        const out = new Set(items.filter(it => this._findItemMatches(st, it, null, words)).map(it => it.id));
        // an installation or a bridge found by its own name brings its devices
        // (only while no filter narrows by what the devices are)
        if (words.length && st.groups && !Object.values(st.sel).some(s => s.size)) {
            for (const g of st.groups) {
                if (words.every(w => this._findWordHit(g.text, w))) g.ids.forEach(id => out.add(id));
            }
        }
        return out;
    },

    // value → how many items would match with it, given the other filters
    _findCounts(st, items, facet) {
        const words = this._findWords(st.q);
        const n = new Map();
        for (const it of items) {
            if (!this._findItemMatches(st, it, facet, words)) continue;
            for (const v of new Set(it.f[facet] || [])) n.set(v, (n.get(v) || 0) + 1);
        }
        return n;
    },

    // the address keeps the view: q, a comma list per filter, view, sort
    _findToQuery(st) {
        const p = new URLSearchParams();
        if (st.q) p.set('q', st.q);
        for (const [f, s] of Object.entries(st.sel)) for (const v of s) p.append(f, v);
        if (st.view && st.view !== st.defaultView) p.set('view', st.view);
        if (st.sort && st.sort !== 'name') p.set('sort', st.sort);
        return p.toString();
    },

    _findFromQuery(st, params) {
        st.q = params.get('q') || '';
        const alias = { system: 'site', gx: 'site', heatpump: 'heat_pump', vfd: 'drive' };
        // the values a filter can take; anything else in a link is dropped and said
        const known = { kind: this._KIND_ORDER, state: ['online', 'offline', 'disabled'], origin: ['builtin', 'custom'] };
        st.ignored = [];
        for (const f of Object.keys(st.sel)) {
            // repeated (kind=a&kind=b); a closed list also takes a comma list
            const raw = params.getAll(f).flatMap(v => (known[f] || f === 'proto') ? v.split(',') : [v])
                .map(v => v.trim()).filter(Boolean)
                .map(v => (f === 'kind' ? alias[v.toLowerCase()] || v.toLowerCase() : v));
            st.sel[f] = new Set(raw.filter(v => !known[f] || known[f].includes(v)));
            raw.filter(v => known[f] && !known[f].includes(v)).forEach(v => st.ignored.push(`${f}=${v}`));
        }
        const view = params.get('view');
        st.view = st.views.includes(view) ? view : st.defaultView;
        st.sort = params.get('sort') || 'name';
    },

    _findState(page) {
        this._find = this._find || {};
        if (!this._find[page]) {
            const facets = page === 'devices' ? ['kind', 'state', 'proto'] : ['kind', 'vendor', 'proto', 'origin'];
            const views = page === 'devices' ? ['tree', 'list'] : ['cards', 'list'];
            this._find[page] = {
                q: '', sel: Object.fromEntries(facets.map(f => [f, new Set()])),
                views, defaultView: views[0], view: views[0], sort: 'name', items: [],
            };
        }
        return this._find[page];
    },

    // ── the bar: search, view switch, filter chips, the count ─────────────
    _findBarHtml(page) {
        const t = (k, d, p) => this.t(k, d, p);
        const st = this._findState(page);
        const id = page === 'devices' ? 'dev' : 'tpl';
        const viewNames = page === 'devices'
            ? { tree: [t('find.view.tree', 'Tree'), 'bi-diagram-3'], list: [t('find.view.list', 'List'), 'bi-list-ul'] }
            : { cards: [t('find.view.cards', 'Cards'), 'bi-grid'], list: [t('find.view.list', 'List'), 'bi-list-ul'] };
        const place = page === 'devices'
            ? t('find.devPlaceholder', 'Search name, id, template, address…')
            : t('find.tplPlaceholder', 'Search name, id, vendor, model…');
        return `
        <div class="find-bar" data-lang="${this._esc(this._lang || 'en')}" role="search" aria-label="${this._esc(page === 'devices' ? t('find.devLabel', 'Find devices') : t('find.tplLabel', 'Find templates'))}" data-find="${page}">
            <div class="find-row">
                <div class="find-search">
                    <i aria-hidden="true" class="bi bi-search"></i>
                    <label class="sr-only" for="${id}FindQ">${this._esc(page === 'devices' ? t('find.devLabel', 'Find devices') : t('find.tplLabel', 'Find templates'))}</label>
                    <input type="search" id="${id}FindQ" ${page === 'templates' ? 'data-alias="tmSearch"' : ''} autocomplete="off" spellcheck="false"
                           placeholder="${this._esc(place)}" value="${this._esc(st.q)}" aria-describedby="${id}FindCount">
                    <kbd class="find-kbd" aria-hidden="true">/</kbd>
                </div>
                <button type="button" class="btn btn-ghost btn-sm find-facets-toggle" aria-expanded="false" aria-controls="${id}FindFacets">
                    <i aria-hidden="true" class="bi bi-funnel"></i> <span>${t('find.filters', 'Filters')}</span><span class="find-active-n"></span></button>
                <div class="find-views" role="group" aria-label="${this._esc(t('find.viewLabel', 'Show as'))}">
                    ${st.views.map(v => `<button type="button" class="btn btn-ghost btn-sm" data-find-view="${v}" aria-pressed="${st.view === v}"><i aria-hidden="true" class="bi ${viewNames[v][1]}"></i> ${this._esc(viewNames[v][0])}</button>`).join('')}
                </div>
            </div>
            <div class="find-on"></div>
            <div class="find-facets" id="${id}FindFacets"></div>
            <div class="find-status">
                <span id="${id}FindCount" class="find-count" role="status" aria-live="polite"></span>
                <span class="find-ignored field-hint" hidden></span>
                <button type="button" class="btn btn-ghost btn-sm find-clear" hidden><i aria-hidden="true" class="bi bi-x-circle"></i> ${t('find.clear', 'Clear filters')}</button>
            </div>
        </div>`;
    },

    _findFacetLabel(page, f) {
        const t = (k, d) => this.t(k, d);
        return ({
            kind: t('find.facet.kind', 'Kind'), state: t('find.facet.state', 'State'),
            proto: t('find.facet.proto', 'Protocol'), vendor: t('find.facet.vendor', 'Vendor'),
            origin: t('find.facet.origin', 'Origin'),
        })[f] || f;
    },

    _findValueLabel(f, v) {
        const t = (k, d) => this.t(k, d);
        if (f === 'kind') return this._kindLabel(v);
        if (f === 'state') return ({ online: t('find.state.online', 'Online'), offline: t('find.state.offline', 'Offline'),
                                     disabled: t('find.state.disabled', 'Disabled') })[v] || v;
        if (f === 'proto') return (v === 'none' && this._protoLabel) ? this._protoLabel(v) : (this._PROTO_LABEL && this._PROTO_LABEL[v]) || ({ modbus: 'Modbus', http: 'HTTP', mqtt: 'MQTT' })[v] || v;
        if (f === 'origin') return v === 'builtin' ? t('find.origin.builtin', 'Built-in') : t('find.origin.custom', 'Mine');
        return v;
    },

    // chips: every value with a count; a value with none hides unless it is on
    _findPaintFacets(page) {
        const st = this._findState(page);
        const bar = document.querySelector(`.find-bar[data-find="${page}"]`);
        const box = bar?.querySelector('.find-facets');
        if (!box) return;
        const order = { kind: this._KIND_ORDER, state: ['online', 'offline', 'disabled'] };
        const MANY = 8;
        box.innerHTML = Object.keys(st.sel).map(f => {
            const counts = this._findCounts(st, st.items, f);
            const vals = [...new Set([...counts.keys(), ...st.sel[f]])];
            if (!vals.length) return '';
            const ord = order[f];
            vals.sort((a, b) => ord ? ord.indexOf(a) - ord.indexOf(b)
                : (counts.get(b) || 0) - (counts.get(a) || 0) || String(a).localeCompare(String(b)));
            const shown = vals.filter(v => counts.get(v) || st.sel[f].has(v));
            if (!shown.length) return '';
            const open = st.moreOpen && st.moreOpen[f];
            const chip = v => {
                const n = counts.get(v) || 0;
                const on = st.sel[f].has(v);
                const icon = f === 'kind' ? `<i aria-hidden="true" class="bi ${this._KIND_ICON[v] || this._KIND_ICON.other}"></i> ` : '';
                return `<button type="button" class="find-chip" data-facet="${f}" data-value="${this._esc(v)}" aria-pressed="${on}">${icon}${this._esc(this._findValueLabel(f, v))} <span class="find-chip-n">${n}</span></button>`;
            };
            const head = shown.length > MANY && !open ? shown.slice(0, MANY).concat(shown.slice(MANY).filter(v => st.sel[f].has(v))) : shown;
            const more = shown.length > head.length
                ? `<button type="button" class="btn btn-ghost btn-sm find-more" data-facet-more="${f}">${this._esc(this.tn('find.more', shown.length - head.length, '{n} more'))}</button>`
                : (open && shown.length > MANY ? `<button type="button" class="btn btn-ghost btn-sm find-more" data-facet-more="${f}">${this._esc(this.t('find.fewer', 'Fewer'))}</button>` : '');
            const gid = `${page}-facet-${f}`;
            return `<div class="find-facet" role="group" aria-labelledby="${gid}"><span class="find-facet-label" id="${gid}">${this._esc(this._findFacetLabel(page, f))}</span>${head.map(chip).join('')}${more}</div>`;
        }).join('')
            // a phone closes the sheet on the result it shows
            + `<button type="button" class="btn btn-primary btn-sm find-sheet-done">${this._esc(this.tn('find.showN', this._findMatched(st, st.items).size, 'Show {n} results'))}</button>`;
        // folded on a phone: what is on stays in sight, each removable
        const on = bar.querySelector('.find-on');
        if (on) {
            on.innerHTML = Object.entries(st.sel).flatMap(([f, set]) => [...set].map(v =>
                `<button type="button" class="find-on-chip" data-on-facet="${f}" data-on-value="${this._esc(v)}"
                     aria-label="${this._esc(this.t('find.removeFilter', 'Remove filter {what}', { what: `${this._findFacetLabel(page, f)}: ${this._findValueLabel(f, v)}` }))}">${this._esc(this._findValueLabel(f, v))} <i aria-hidden="true" class="bi bi-x"></i></button>`)).join('');
        }
        const note = bar.querySelector('.find-ignored');
        if (note) {
            note.hidden = !(st.ignored && st.ignored.length);
            note.textContent = note.hidden ? '' : this.t('find.ignored', 'Not a filter here, left out: {list}', { list: st.ignored.join(', ') });
        }
        const active = Object.values(st.sel).reduce((n, s) => n + s.size, 0);
        const an = bar.querySelector('.find-active-n');
        if (an) an.textContent = active ? ` (${active})` : '';
        const clear = bar.querySelector('.find-clear');
        if (clear) clear.hidden = !this._findFiltering(st);
    },

    // the count is said once the typing settles (a live region that speaks
    // on every key would talk over the user)
    _findSayCount(page, shown, total) {
        const st = this._findState(page);
        const el = document.getElementById(page === 'devices' ? 'devFindCount' : 'tplFindCount');
        if (!el) return;
        const what = page === 'devices'
            ? this.tn('find.nDevices', total, '{n} devices')
            : this.tn('find.nTemplates', total, '{n} templates');
        const text = this._findFiltering(st)
            ? this.t('find.showing', 'Showing {shown} of {total}', { shown, total: what })
            : what;
        clearTimeout(st._sayT);
        st._sayT = setTimeout(() => { el.textContent = text; }, 350);
    },

    // one listener set per bar (the bar is drawn once per page render)
    _findWireBar(page, apply) {
        const st = this._findState(page);
        const bar = document.querySelector(`.find-bar[data-find="${page}"]`);
        if (!bar || bar._wired) return;
        bar._wired = true;
        const q = bar.querySelector('input[type=search]');
        let raf = 0;
        q.addEventListener('input', () => {
            st.q = q.value;
            cancelAnimationFrame(raf);
            raf = requestAnimationFrame(() => { apply(); this._findRemember(page); });
        });
        q.addEventListener('keydown', e => {
            if (e.key === 'Escape' && q.value) { e.preventDefault(); e.stopPropagation(); q.value = ''; st.q = ''; apply(); this._findRemember(page); }
        });
        bar.addEventListener('click', e => {
            const onChip = e.target.closest('.find-on-chip');
            if (onChip) {
                st.sel[onChip.dataset.onFacet]?.delete(onChip.dataset.onValue);
                apply();
                this._findRemember(page);
                (bar.querySelector('.find-on-chip') || bar.querySelector('.find-facets-toggle') || q).focus();
                return;
            }
            const chip = e.target.closest('.find-chip');
            if (chip) {
                const s = st.sel[chip.dataset.facet];
                const v = chip.dataset.value;
                if (s.has(v)) s.delete(v); else s.add(v);
                apply();
                this._findRemember(page);
                // the chips were redrawn: keep the focus on the same chip
                bar.querySelector(`.find-chip[data-facet="${CSS.escape(chip.dataset.facet)}"][data-value="${CSS.escape(v)}"]`)?.focus();
                return;
            }
            const more = e.target.closest('[data-facet-more]');
            if (more) {
                st.moreOpen = { ...(st.moreOpen || {}), [more.dataset.facetMore]: !(st.moreOpen || {})[more.dataset.facetMore] };
                this._findPaintFacets(page);
                bar.querySelector(`[data-facet-more="${CSS.escape(more.dataset.facetMore)}"]`)?.focus();
                return;
            }
            const vb = e.target.closest('[data-find-view]');
            if (vb) {
                st.view = vb.dataset.findView;
                bar.querySelectorAll('[data-find-view]').forEach(b => b.setAttribute('aria-pressed', String(b === vb)));
                apply(true);
                this._findRemember(page);
                return;
            }
            if (e.target.closest('.find-clear')) {
                st.q = ''; q.value = '';
                Object.values(st.sel).forEach(s => s.clear());
                apply();
                this._findRemember(page);
                q.focus();
                return;
            }
            if (e.target.closest('.find-sheet-done')) {
                const t2 = bar.querySelector('.find-facets-toggle');
                t2?.setAttribute('aria-expanded', 'false');
                bar.classList.remove('facets-open');
                t2?.focus();
                return;
            }
            const tg = e.target.closest('.find-facets-toggle');
            if (tg) {
                const open = tg.getAttribute('aria-expanded') !== 'true';
                tg.setAttribute('aria-expanded', String(open));
                bar.classList.toggle('facets-open', open);
            }
        });
    },

    // the view goes into the address without a new history entry
    _findRemember(page) {
        if (this._routeReplace) this._routeReplace();
    },

    // "/" puts the cursor in the search of the page in front
    _findWireSlash() {
        if (this._findSlashWired) return;
        this._findSlashWired = true;
        document.addEventListener('keydown', e => {
            if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
            const a = document.activeElement;
            if (a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return;
            if (document.querySelector('.modal.active')) return;
            const id = this.currentPage === 'devices' && document.getElementById('devicesListView')?.style.display !== 'none' ? 'devFindQ'
                : this.currentPage === 'templates' ? 'tplFindQ' : null;
            const el = id && document.getElementById(id);
            if (!el || !el.offsetParent) return;
            e.preventDefault();
            el.focus();
            el.select();
        });
    },

    // ── Devices ───────────────────────────────────────────────────────────
    _devState(e) {
        if (e.enabled === false) return 'disabled';
        return e.connected ? 'online' : 'offline';
    },

    // a unit read through its installation's sources is read the way those
    // sources are (an HTTP-only unit is not TCP) — the server's own rule
    _devProtos(e) {
        const via = (e.read_via || []).map(r => r.protocol).filter(Boolean);
        return [...new Set(via.length ? via : [e.protocol].filter(Boolean))];
    },

    // what a device is found by, from the list the page already has
    _devFindIndex(devices, endpoints, bridges) {
        const st = this._findState('devices');
        const epName = Object.fromEntries((endpoints || []).map(p => [p.id, p.name || p.id]));
        const byId = Object.fromEntries((devices || []).map(d => [d.id, d]));
        const fold = x => this._fold(x);
        st.items = (devices || []).map(d => ({
            id: d.id,
            f: { kind: [d.kind || 'other'], state: [this._devState(d)], proto: this._devProtos(d) },
            text: fold([d.id, d.name, d.template, d.host, d.http_url, d.mqtt_in_topic, d.mqtt_in_broker,
                        d.endpoint_id, epName[d.endpoint_id], d.component_of, byId[d.component_of]?.name,
                        d.bridge, d.kind, this._kindLabel(d.kind), this._KIND_WORDS[d.kind || 'other'],
                        ...this._devProtos(d),
                        // searched in either language: "unit 3" and the page's own word
                        d.unit_id != null ? `unit ${d.unit_id} ${this.t('common.unitN', 'unit {n}', { n: d.unit_id })}` : '',
                        ...(d.read_via || []).flatMap(r => [r.id, r.address])]
                .filter(x => x !== undefined && x !== null && x !== '').join(' ')),
        }));
        st.byId = byId;
        st.itemById = new Map(st.items.map(it => [it.id, it]));
        // what an installation's or a bridge's own name brings along
        st.groups = [
            ...(endpoints || []).map(p => ({ text: fold(`${p.id} ${p.name || ''}`),
                ids: (devices || []).filter(d => d.endpoint_id === p.id).map(d => d.id) })),
            ...(bridges || []).map(b => ({ text: fold(`${b.id} ${b.name || ''} ${b.host || ''}`),
                ids: (devices || []).filter(d => d.bridge === b.id).map(d => d.id) })),
        ];
        st.epName = epName;
    },

    // drawn after every list render: the bar once (again after a language
    // switch), then the filter applied
    _devFindAfterRender() {
        const host = document.getElementById('devFind');
        if (!host) return;
        const bar = host.querySelector('.find-bar');
        if (!bar || bar.dataset.lang !== (this._lang || 'en')) {
            host.innerHTML = this._findBarHtml('devices');
            this._findWireBar('devices', (viewChanged) => this._devFindApply(viewChanged));
            this._findWireSlash();
            const empty = document.getElementById('devFindEmpty');
            if (empty) empty.innerHTML = '';
        } else {
            // a re-render keeps what was typed (another tab may have rebuilt the list)
            const q = document.getElementById('devFindQ');
            const st = this._findState('devices');
            if (q && q.value !== st.q) q.value = st.q;
            host.querySelectorAll('[data-find-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.findView === st.view)));
        }
        this._devFindApply(true);
    },

    _devFindApply(viewChanged = false) {
        const st = this._findState('devices');
        const list = document.getElementById('devicesList');
        const table = document.getElementById('devicesTable');
        const empty = document.getElementById('devFindEmpty');
        if (!list) return;
        const filtering = this._findFiltering(st);
        const matched = this._findMatched(st, st.items);

        if (st.view === 'list') {
            list.hidden = true;
            if (table) {
                table.hidden = false;
                if (viewChanged || !table.firstChild || table._for !== st.items) this._devTableRender();
                table.querySelectorAll('tr[data-f]').forEach(r => { r.hidden = !matched.has(r.dataset.f); });
            }
        } else {
            list.hidden = false;
            if (table) { table.hidden = true; }
            this._devTreeFilter(list, matched, filtering);
        }
        this._devFindRestorable(filtering);
        const shown = matched.size;
        if (empty) {
            empty.hidden = !(filtering && !shown);
            if (!empty.hidden && !empty.firstChild) {
                empty.innerHTML = `<i aria-hidden="true" class="bi bi-search"></i>
                    <div><b>${this._esc(this.t('find.noMatch', 'Nothing matches'))}</b>
                    <div class="field-hint">${this._esc(this.t('find.noMatchHint', 'Try fewer words, or clear the filters.'))}</div></div>
                    <button type="button" class="btn btn-secondary btn-sm" data-find-clear-empty>${this._esc(this.t('find.clear', 'Clear filters'))}</button>`;
                empty.querySelector('[data-find-clear-empty]').addEventListener('click', () =>
                    document.querySelector('.find-bar[data-find="devices"] .find-clear')?.click());
            }
        }
        this._findPaintFacets('devices');
        this._findSayCount('devices', shown, st.items.length);
    },

    // deleted devices: folded under the list; while searching only those
    // that match show, and the section only when one does
    _devFindRestorable(filtering) {
        const box = document.getElementById('restorableDevices');
        if (!box) return;
        const det = box.querySelector('details');
        if (!det) return;
        const words = this._findWords(this._findState('devices').q);
        const facets = Object.values(this._findState('devices').sel).some(s => s.size);
        let any = false;
        det.querySelectorAll('[data-restorable]').forEach(r => {
            const hit = !filtering || (!facets && words.every(w => this._findWordHit(r.dataset.text || '', w)));
            r.hidden = !hit;
            any = any || hit;
        });
        box.hidden = filtering && !any;
        if (filtering && any) det.open = true;
    },

    // a row shown only as the frame of what matched inside it: marked so,
    // in words too, and never counted
    _findMarkContext(row, on, n) {
        if (!row) return;
        row.classList.toggle('find-context', on);
        let pill = row.querySelector('.find-ctx-pill');
        if (on) {
            const text = this.tn('find.contains', n, 'contains {n} matches');
            if (!pill) {
                pill = document.createElement('span');
                pill.className = 'find-ctx-pill';
                (row.querySelector('.device-row-title') || row).append(pill);
            }
            pill.textContent = text;
        } else if (pill) pill.remove();
    },

    // keeps a disclosure's state, its chevron and aria-expanded in step
    _findDisclose(toggle, box, open) {
        if (box) box.style.display = open ? '' : 'none';
        if (toggle) {
            toggle.setAttribute('aria-expanded', String(open));
            const chev = toggle.querySelector('.endpoint-chevron, .component-chevron');
            if (chev) chev.style.transform = open ? 'rotate(90deg)' : '';
        }
    },

    // the tree: rows hide, a group shows while anything in it matches (and
    // opens while it does), a parent stays as context for a matching part
    _devTreeFilter(list, matched, filtering) {
        list.querySelectorAll('.device-row[data-f]').forEach(r => {
            r.hidden = filtering && !matched.has(r.dataset.f);
            this._findMarkContext(r, false);
        });
        // components under their parent
        list.querySelectorAll('[data-component-units]').forEach(box => {
            const pid = box.dataset.componentUnits;
            const kids = [...box.querySelectorAll('.device-row[data-f]')];
            const nHit = kids.filter(r => matched.has(r.dataset.f)).length;
            const tog = list.querySelector(`[data-component-toggle="${CSS.escape(pid)}"]`);
            const parentRow = list.querySelector(`#devicesList > .device-row[data-f="${CSS.escape(pid)}"]`);
            if (!filtering) {
                if (tog) tog.hidden = false;
                this._findDisclose(tog, box, this._componentOpenState(pid));
                return;
            }
            const parentHit = matched.has(pid);
            if (tog) tog.hidden = !(nHit || parentHit);
            this._findDisclose(tog, box, nHit > 0);
            if (nHit && parentRow && !parentHit) { parentRow.hidden = false; this._findMarkContext(parentRow, true, nHit); }
        });
        // installations: the header and its units
        list.querySelectorAll('[data-endpoint-toggle]').forEach(h => {
            const pid = h.dataset.endpointToggle;
            const box = list.querySelector(`[data-endpoint-units="${CSS.escape(pid)}"]`);
            const units = box ? [...box.querySelectorAll('.device-row[data-f]')] : [];
            const nHit = units.filter(r => matched.has(r.dataset.f)).length;
            const btn = h.querySelector('[data-endpoint-disclose]');
            this._findMarkContext(h, false);
            if (!filtering) {
                h.hidden = false;
                if (box) box.hidden = false;
                this._findDisclose(btn, box, this._endpointOpenState(pid));
                return;
            }
            h.hidden = !nHit;
            if (box) box.hidden = !nHit;
            this._findDisclose(btn, box, nHit > 0);
            // shown for its units (all of them when its own name matched)
            if (nHit && nHit < units.length) this._findMarkContext(h, true, nHit);
        });
        // bridges: a bus shows while one of its devices matches
        const cards = [...list.querySelectorAll('.bridge-card[data-bridge]')];
        cards.forEach(c => {
            let anyCard = false;
            c.querySelectorAll('.bridge-port').forEach(p => {
                let any = false;
                p.querySelectorAll('.bridge-dev[data-f]').forEach(d => {
                    const hit = !filtering || matched.has(d.dataset.f);
                    d.hidden = !hit;
                    any = any || hit;
                });
                p.hidden = filtering && !any;
                anyCard = anyCard || !p.hidden;
            });
            c.hidden = filtering && !anyCard;
        });
        const bar = list.querySelector('.bridges-bar');
        if (bar) bar.hidden = filtering && !cards.some(c => !c.hidden);
    },

    // the list: one sortable table, every device (parts and units too)
    _devTableRender() {
        const st = this._findState('devices');
        const box = document.getElementById('devicesTable');
        if (!box) return;
        const t = (k, d, p) => this.t(k, d, p);
        const cols = [
            ['name', t('find.col.name', 'Name')], ['kind', t('find.facet.kind', 'Kind')],
            ['state', t('find.facet.state', 'State')], ['proto', t('find.facet.proto', 'Protocol')],
            ['template', t('find.col.template', 'Template')], ['age', t('find.col.seen', 'Last value')],
            ['part', t('find.col.partOf', 'Part of')],
        ];
        const sort = st.sort || 'name';
        const desc = sort.startsWith('-');
        const key = sort.replace(/^-/, '');
        const hc = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                     down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        const partOf = d => d.endpoint_id ? (st.epName[d.endpoint_id] || d.endpoint_id)
            : d.component_of ? (st.byId[d.component_of]?.name || d.component_of)
            : d.bridge ? `${d.bridge} :${d.bridge_port ?? ''}` : '';
        const rows = (st.items || []).map(it => st.byId[it.id]).filter(Boolean);
        const val = (d, k) => ({
            name: String(d.name || d.id).toLowerCase(), kind: this._KIND_ORDER.indexOf(d.kind || 'other'),
            state: ['online', 'offline', 'disabled'].indexOf(this._devState(d)), proto: this._devProtos(d).join(','),
            template: String(d.template || ''), age: d.staleness_age_s ?? Infinity, part: partOf(d).toLowerCase(),
        })[k];
        rows.sort((a, b) => {
            const x = val(a, key), y = val(b, key);
            const c = (typeof x === 'number' && typeof y === 'number') ? x - y : String(x).localeCompare(String(y));
            return (desc ? -c : c) || String(a.name || a.id).localeCompare(String(b.name || b.id));
        });
        const ariaSort = k => k === key ? (desc ? 'descending' : 'ascending') : 'none';
        box.innerHTML = `
        <div class="find-table-wrap">
        <table class="find-table">
            <caption class="sr-only">${this._esc(t('find.devTable', 'Devices, one row each'))}</caption>
            <thead><tr>${cols.map(([k, label]) => `<th scope="col" aria-sort="${ariaSort(k)}"><button type="button" class="find-sort" data-sort="${k}">${this._esc(label)}<i aria-hidden="true" class="bi ${k === key ? (desc ? 'bi-caret-down-fill' : 'bi-caret-up-fill') : 'bi-chevron-expand'}"></i></button></th>`).join('')}</tr></thead>
            <tbody>${rows.map(d => `
                <tr data-f="${this._esc(d.id)}">
                    <td><span class="status-dot" style="--dot:${hc[d.data_health] || hc.idle}" aria-hidden="true"></span>
                        <a href="#/devices/${encodeURIComponent(d.id)}" class="find-open">${this._esc(d.name || d.id)}</a>
                        <span class="dev-chip">${this._esc(d.id)}</span></td>
                    <td>${this._kindCellHtml(d.kind)}</td>
                    <td>${this._esc(this._verdictWord(d))}</td>
                    <td>${this._esc(this._devProtos(d).map(p => this._protoLabel(p)).join(' + '))}</td>
                    <td>${this._esc(d.template || '—')}</td>
                    <td class="num">${d.enabled === false ? '—' : d.staleness_age_s != null ? this._esc(t('find.ago', '{s} s ago', { s: d.staleness_age_s })) : '—'}</td>
                    <td>${this._esc(partOf(d) || '—')}</td>
                </tr>`).join('')}</tbody>
        </table></div>`;
        box._for = st.items;
        box.querySelectorAll('[data-sort]').forEach(b => b.addEventListener('click', () => {
            const k = b.dataset.sort;
            st.sort = (st.sort || 'name') === k ? `-${k}` : k;
            this._devTableRender();
            this._devFindApply();
            this._findRemember('devices');
            box.querySelector(`[data-sort="${CSS.escape(k)}"]`)?.focus();
        }));
    },

    // the 5 s status poll paints the dots in place (tree, list, buses) — no
    // rebuild, no row moving under the pointer; a filter is applied again on
    // the next action, its counts follow now unless the user is in the bar
    _devFindLivePaint(statusDevices) {
        if (this.currentPage !== 'devices') return;
        const list = document.getElementById('devicesList');
        if (!list || !statusDevices) return;
        const st = this._findState('devices');
        const hc = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                     down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,var(--text-secondary))' };
        const table = document.getElementById('devicesTable');
        for (const s of statusDevices) {
            const it = st.itemById && st.itemById.get(s.id);
            if (it) it.f.state = [this._devState(s)];
            const d = st.byId && st.byId[s.id];
            if (d) Object.assign(d, { connected: s.connected, data_health: s.data_health, enabled: s.enabled, staleness_age_s: s.staleness_age_s });
            const color = hc[s.data_health] || hc.idle;
            const word = this._verdictWord(s);
            const sel = CSS.escape(s.id);
            const dots = [
                list.querySelector(`.device-row[data-f="${sel}"] > .status-dot`),
                list.querySelector(`.bridge-dev[data-f="${sel}"] > .status-dot`),
                table && table.querySelector(`tr[data-f="${sel}"] .status-dot`),
            ];
            dots.forEach(dot => { if (dot) { dot.style.setProperty('--dot', color); dot.title = word; } });
            const tr = table && table.querySelector(`tr[data-f="${sel}"]`);
            if (tr) {
                const cells = tr.children;
                if (cells[2]) cells[2].textContent = word;
                if (cells[5]) cells[5].textContent = s.enabled === false ? '—'
                    : s.staleness_age_s != null ? this.t('find.ago', '{s} s ago', { s: s.staleness_age_s }) : '—';
            }
        }
        const bar = document.querySelector('.find-bar[data-find="devices"]');
        if (bar && !bar.contains(document.activeElement)) this._findPaintFacets('devices');
    },

    // ── Templates (device maps) ───────────────────────────────────────────
    _tplFindIndex(tpls) {
        const st = this._findState('templates');
        st.items = (tpls || []).map(x => ({
            id: x.id,
            f: {
                kind: [x.device_kind || 'other'],
                vendor: x.vendor ? [String(x.vendor)] : [],
                proto: x.transport ? [String(x.transport).toLowerCase()] : [],
                origin: [x.builtin ? 'builtin' : 'custom'],
            },
            text: this._fold([x.id, x.name, x.vendor, x.model, x.device_kind, this._kindLabel(x.device_kind),
                              this._KIND_WORDS[x.device_kind || 'other'], x.transport].filter(Boolean).join(' ')),
        }));
        st.byId = Object.fromEntries((tpls || []).map(x => [x.id, x]));
    },

    _tplFindApply(viewChanged = false) {
        const st = this._findState('templates');
        const panel = document.querySelector('[data-tmpanel="devmaps"]');
        if (!panel) return;
        const cards = panel.querySelector('.tm-cards');
        const table = panel.querySelector('.tm-table');
        const empty = panel.querySelector('.tm-find-empty');
        const filtering = this._findFiltering(st);
        const matched = this._findMatched(st, st.items);
        if (st.view === 'list') {
            if (cards) cards.hidden = true;
            if (table) {
                table.hidden = false;
                if (viewChanged || !table.firstChild) this._tplTableRender(table);
                table.querySelectorAll('tr[data-f]').forEach(r => { r.hidden = !matched.has(r.dataset.f); });
            }
        } else {
            if (table) table.hidden = true;
            if (cards) {
                cards.hidden = false;
                cards.querySelectorAll('[data-f]').forEach(c => { c.hidden = filtering && !matched.has(c.dataset.f); });
            }
        }
        if (empty) empty.hidden = !(filtering && !matched.size);
        this._findPaintFacets('templates');
        this._findSayCount('templates', matched.size, st.items.length);
    },

    _tplTableRender(box) {
        const st = this._findState('templates');
        const t = (k, d, p) => this.t(k, d, p);
        const sort = st.sort || 'name';
        const desc = sort.startsWith('-');
        const key = sort.replace(/^-/, '');
        const cols = [['name', t('find.col.name', 'Name')], ['kind', t('find.facet.kind', 'Kind')],
            ['vendor', t('find.facet.vendor', 'Vendor')], ['proto', t('find.facet.proto', 'Protocol')],
            ['origin', t('find.facet.origin', 'Origin')], ['used', t('find.col.usedBy', 'Used by')],
            ['regs', t('templates.regs', 'registers')]];
        const rows = (st.items || []).map(it => st.byId[it.id]).filter(Boolean);
        const val = (x, k) => ({
            name: String(x.name || x.id).toLowerCase(), kind: this._KIND_ORDER.indexOf(x.device_kind || 'other'),
            vendor: String(x.vendor || '').toLowerCase(), proto: String(x.transport || ''),
            origin: x.builtin ? 0 : 1, used: x.used_by_count ?? (x.used_by || []).length, regs: x.registers || 0,
        })[k];
        rows.sort((a, b) => {
            const x = val(a, key), y = val(b, key);
            const c = (typeof x === 'number' && typeof y === 'number') ? x - y : String(x).localeCompare(String(y));
            return (desc ? -c : c) || String(a.name || a.id).localeCompare(String(b.name || b.id));
        });
        const ariaSort = k => k === key ? (desc ? 'descending' : 'ascending') : 'none';
        box.innerHTML = `
        <div class="find-table-wrap">
        <table class="find-table">
            <caption class="sr-only">${this._esc(t('find.tplTable', 'Templates, one row each'))}</caption>
            <thead><tr>${cols.map(([k, label]) => `<th scope="col" aria-sort="${ariaSort(k)}"><button type="button" class="find-sort" data-sort="${k}">${this._esc(label)}<i aria-hidden="true" class="bi ${k === key ? (desc ? 'bi-caret-down-fill' : 'bi-caret-up-fill') : 'bi-chevron-expand'}"></i></button></th>`).join('')}</tr></thead>
            <tbody>${rows.map(x => `
                <tr data-f="${this._esc(x.id)}">
                    <td><button type="button" class="find-open link-btn" data-tm-view="${this._esc(x.id)}">${this._esc(x.name || x.id)}</button> <span class="dev-chip">${this._esc(x.id)}</span></td>
                    <td>${this._kindCellHtml(x.device_kind)}</td>
                    <td>${this._esc(x.vendor || '—')}</td>
                    <td>${this._esc(this._findValueLabel('proto', String(x.transport || '').toLowerCase()) || '—')}</td>
                    <td>${this._esc(this._findValueLabel('origin', x.builtin ? 'builtin' : 'custom'))}</td>
                    <td class="num">${x.used_by_count ?? (x.used_by || []).length}</td>
                    <td class="num">${x.registers ?? '—'}</td>
                </tr>`).join('')}</tbody>
        </table></div>`;
        box.querySelectorAll('[data-sort]').forEach(b => b.addEventListener('click', () => {
            const k = b.dataset.sort;
            st.sort = (st.sort || 'name') === k ? `-${k}` : k;
            this._tplTableRender(box);
            this._tplFindApply();
            this._findRemember('templates');
            box.querySelector(`[data-sort="${CSS.escape(k)}"]`)?.focus();
        }));
        box.querySelectorAll('[data-tm-view]').forEach(b => b.addEventListener('click', () => this.viewTemplate(b.dataset.tmView)));
    },

    // ── a device's kind, set by an administrator ──────────────────────────
    async setDeviceKind(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const dev = (this._devices || []).find(d => d.id === id) || {};
        const own = dev.kind_source === 'override' ? dev.kind : '';
        const derived = dev.kind_source === 'override' ? '' : (dev.kind || 'other');
        // a device measured only by its meters: the kinds the gateway allows
        // for it, and nothing to derive from (no template)
        const mo = dev.protocol === 'none'
            ? ((await (this._meterRoles ? this._meterRoles() : null)) || {}).meter_only_kinds : null;
        const options = mo
            ? [...(mo.first || []), ...(mo.more || [])].map(k => ({ value: k, label: this._kindLabel(k) }))
            : [{ value: '', label: t('kind.derive', 'Not set — derive it ({kind})', { kind: this._kindLabel(derived || 'other') }) }]
                .concat(this._KIND_ORDER.filter(k => k !== 'other').map(k => ({ value: k, label: this._kindLabel(k) })));
        let result = null;
        const got = await this.askValues({
            title: t('kind.setTitle', 'Kind of {name}', { name: dev.name || id }),
            message: t('kind.setMsg', 'How the device is filed and found. It changes no reading, topic or history.'),
            fields: [{ name: 'kind', label: t('find.facet.kind', 'Kind'), kind: 'select', options,
                       initial: mo ? (dev.kind || options[0]?.value) : own, required: !!mo }],
            confirmLabel: t('common.save', 'Save'),
            submit: async v => {
                const r = await fetch(`/api/devices/${encodeURIComponent(id)}/organise`, {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ kind: v.kind || null }),
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) return this._respError(r, d);
                result = d;
                return null;
            },
        });
        if (!got || !result) return;
        this.showToast('success', t('kind.saved', 'Kind saved'),
            `${dev.name || id}: ${this._kindLabel(result.kind)} — ${this._kindSourceWords(result.kind_source)}`);
        await this._fetchDevices(true);
        if (this._devDetail && this._devDetail.id === id) await this.openDeviceDetail(id);
    },
});
