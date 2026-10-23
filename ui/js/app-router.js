/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// router — augments JanitzaMonitor.prototype
//
// Where you are lives in the address: a page, a device and its tab, an
// installation, a list's search and filters. Back and Forward walk it, a
// reload lands on the same view, a link opens it.
//
//   #/devices?q=…&kind=meter,inverter&state=online&view=list&sort=-name
//   #/devices/<id>[/<tab>]
//   #/installations/<id>
//   #/templates?q=…&vendor=…&view=list
//   #/<page>                  (dashboard, monitor, rules, settings…)
//
// The pages keep their own functions (navigateTo, openDeviceDetail…): this
// only follows them, and calls them when the address changes. One user step
// is one history entry, however many of those functions it runs. The view
// on screen is said by whoever shows it (`this._view`); the address is read
// from that, never guessed.

Object.assign(JanitzaMonitor.prototype, {

    _ROUTE_PAGES: ['dashboard', 'monitor', 'history', 'energy', 'pq', 'devices', 'templates', 'vmeters',
                   'rules', 'status', 'diagnostics', 'config'],

    _routeParse(hash) {
        const h = String(hash || '').replace(/^#/, '');
        if (!h.startsWith('/')) return null;            // "#" or an in-page anchor: not a route
        const [path, query = ''] = h.slice(1).split('?');
        const parts = path.split('/').filter(Boolean).map(p => { try { return decodeURIComponent(p); } catch (e) { return p; } });
        return { parts, params: new URLSearchParams(query) };
    },

    // the address of what is on screen now
    _routeNow() {
        const page = this.currentPage || 'dashboard';
        if (page === 'devices') {
            const v = this._view || { type: 'list' };
            if (v.type === 'device' || (v.type === 'missing' && v.what === 'device')) {
                const tab = v.type === 'device'
                    ? document.querySelector('#deviceWsTabs .config-main-tab.active[data-dtab]')?.dataset.dtab : '';
                return `#/devices/${encodeURIComponent(v.id)}${tab && tab !== 'overview' ? '/' + encodeURIComponent(tab) : ''}`;
            }
            if (v.type === 'installation' || (v.type === 'missing' && v.what === 'installation')) {
                return `#/installations/${encodeURIComponent(v.id)}`;
            }
            const q = this._find?.devices ? this._findToQuery(this._find.devices) : '';
            return `#/devices${q ? '?' + q : ''}`;
        }
        if (page === 'templates') {
            const q = this._find?.templates ? this._findToQuery(this._find.templates) : '';
            return `#/templates${q ? '?' + q : ''}`;
        }
        return `#/${page}`;
    },

    // record the view: a new history entry for a new place (the entry left
    // keeps its scroll and what was opened from it), the same entry for a
    // search or a tab change
    _routeSync(push) {
        if (this._routeApplying || this._routePending || !this._routeReady) return;
        const h = this._routeNow();
        if (location.hash === h) return;
        try {
            if (push) {
                const leave = this._routeLeave || {};
                history.replaceState({ ...(history.state || {}), scrollY: leave.scrollY ?? window.scrollY,
                                       opened: this._view?.id || null }, '', location.hash);
                history.pushState(null, '', h);
            } else {
                history.replaceState(history.state, '', h);
            }
        } catch (e) { return; }
        this._routeLast = h;
        this._routeLeave = null;
        if (push) this._routeAnnounce();
    },

    _routeReplace() {
        clearTimeout(this._routeReplaceT);
        this._routeReplaceT = setTimeout(() => this._routeSync(false), 250);
    },

    // several functions fire for one user step (a page, then its device):
    // they make one history entry, written when the last of them settles
    _routeSoon(push) {
        this._routePush = this._routePush || push;
        if (this._routeSoonT) return;
        this._routeSoonT = setTimeout(() => {
            const p = this._routePush;
            this._routeSoonT = 0;
            this._routePush = false;
            this._routeSync(p);
        }, 0);
    },

    // the page's name in the tab title, and the focus on its heading, so a
    // screen reader hears where it landed (never on the first load)
    _routeHeading() {
        const v = this._view || {};
        if (this.currentPage === 'devices' && v.type && v.type !== 'list') {
            return document.querySelector('#deviceDetailView .section-header h2');
        }
        const page = document.getElementById(`${this.currentPage}Page`);
        return page && page.querySelector('.section-header h2, h2, h3');
    },

    _routeAnnounce({ focus = true } = {}) {
        if (!this._routeBaseTitle) this._routeBaseTitle = document.title || 'Multi-Bus Gateway';
        const h = this._routeHeading();
        // the heading's words without its id chips and screen-reader extras
        let name = '';
        if (h) {
            const c = h.cloneNode(true);
            c.querySelectorAll('.dev-chip, .sr-only, button, nav > span[aria-hidden]').forEach(x => x.remove());
            // the device page names itself (.ddv-name); elsewhere the last crumb
            const own = c.querySelector('.ddv-name');
            const crumbs = c.querySelectorAll('nav a, nav span');
            name = (own ? own.textContent : crumbs.length ? crumbs[crumbs.length - 1].textContent : c.textContent).replace(/\s+/g, ' ').trim();
        }
        const tab = document.querySelector(`.nav-tab[data-page="${this.currentPage}"]`)?.textContent.trim() || '';
        document.title = `${name || tab || ''}${name || tab ? ' — ' : ''}${this._routeBaseTitle}`;
        if (focus && h) {
            if (!h.hasAttribute('tabindex')) h.setAttribute('tabindex', '-1');
            h.focus({ preventScroll: true });
        }
    },

    // a link that names nothing: said plainly, with the way back
    _routeMissing(what, id) {
        const t = (k, d, p) => this.t(k, d, p);
        if (this.currentPage !== 'devices') this.navigateTo('devices', { _opening: true });
        this._leaveDeviceDetail?.();
        this._stopEndpointDetail?.();
        this._devDetail = null;
        document.getElementById('devicesListView').style.display = 'none';
        document.getElementById('deviceRegistersView').style.display = 'none';
        const view = document.getElementById('deviceDetailView');
        view.style.display = '';
        const title = what === 'device'
            ? t('route.noDevice', 'No device "{id}" here', { id })
            : t('route.noInstallation', 'No installation "{id}" here', { id });
        view.innerHTML = `
        <div class="section-header"><h2 tabindex="-1"><i aria-hidden="true" class="bi bi-question-circle"></i> ${this._esc(title)}</h2></div>
        <div class="settings-card"><div class="settings-card-body route-missing">
            <p>${this._esc(t('route.missingHint', 'It may have been deleted or renamed, or the link is mistyped. A deleted device can be restored from the Devices page.'))}</p>
            <a class="btn btn-primary btn-sm" href="#/devices"><i aria-hidden="true" class="bi bi-arrow-left"></i> ${this._esc(t('route.backToDevices', 'Back to devices'))}</a>
        </div></div>`;
        this._view = { type: 'missing', what, id };
    },

    // the address changed (Back, Forward, a typed or opened link): go there
    async _routeApply({ initial = false } = {}) {
        const r = this._routeParse(location.hash);
        if (!r || location.hash === this._routeLast) return;
        this._routeLast = location.hash;
        const [first, id, tab] = r.parts;
        const state = history.state || {};
        this._routeApplying = (this._routeApplying || 0) + 1;
        let restore = null;
        try {
            if (first === 'devices' && id) {
                // the same device, another tab: switch it, do not reopen
                const same = this._view?.type === 'device' && this._view.id === id && this.currentPage === 'devices';
                const ok = same || await this.openDeviceDetail(id);
                if (ok === false) this._routeMissing('device', id);
                else {
                    // `edit` is the earlier name of `connection`; a group's
                    // name opens its tab; a tab this account may not open —
                    // by role, not by sight — lands on the Overview
                    let want = tab || 'overview';
                    want = (this._DTAB_ALIAS || {})[want] || want;
                    if ((this._DGROUPS || []).includes(want) && this._dgroupTab) want = this._dgroupTab(want);
                    // a tab this page does not have is said, not silently swapped
                    if (tab && this._DTAB_GROUP && !(want in this._DTAB_GROUP)) {
                        this.showToast('info', this.t('route.noTab', 'No such tab'),
                            this.t('route.noTabMsg', 'This device has no "{tab}" tab — showing its Overview.', { tab }));
                        want = 'overview';
                    }
                    const btn = document.querySelector(`#deviceWsTabs [data-dtab="${CSS.escape(want)}"]`);
                    if (this._switchDeviceTab) this._switchDeviceTab(btn && this._dtabAllowed(btn) ? want : 'overview');
                }
            } else if (first === 'installations' && id && this.openEndpointDetail) {
                const ok = await this.openEndpointDetail(id);
                if (ok === false) this._routeMissing('installation', id);
            } else if (first === 'devices') {
                this._findFromQuery(this._findState('devices'), r.params);
                this.navigateTo('devices', { _opening: true, _fromRoute: true });
                this._showDevicesList();
                await this.renderDevicesList();
                restore = state;
            } else if (first === 'templates') {
                this._findFromQuery(this._findState('templates'), r.params);
                this.navigateTo('templates', { fromNav: true, _fromRoute: true });
            } else if (this._ROUTE_PAGES.includes(first)) {
                this.navigateTo(first, { _fromRoute: true });
            }
        } catch (e) {
            console.error('route', location.hash, e);
        } finally {
            this._routeApplying -= 1;
        }
        if (this._routeApplying) return;
        // what the page settled on, in its own words
        const h = this._routeNow();
        if (h !== location.hash) { try { history.replaceState(history.state, '', h); } catch (e) { /* */ } }
        this._routeLast = h;
        this._routeAnnounce({ focus: !initial });
        // back on a list: where it was scrolled, the row it was left from
        if (restore && restore.scrollY != null && !initial) {
            window.scrollTo(0, restore.scrollY);
            // the one shown (the tree and the list both hold the device)
            const row = restore.opened && [...document.querySelectorAll(
                `#devicesList [data-f="${CSS.escape(restore.opened)}"] .device-row-name, #devicesTable tr[data-f="${CSS.escape(restore.opened)}"] a`)]
                .find(el => el.offsetParent);
            if (row) row.focus({ preventScroll: true });
        }
    },

    // after init: follow the address the page was opened with, then listen
    async _routeStart() {
        if (this._routeReady) return;
        this._routeBaseTitle = document.title || 'Multi-Bus Gateway';
        window.addEventListener('popstate', () => this._routeApply());
        window.addEventListener('hashchange', () => this._routeApply());
        // a plain click on an in-app link goes the router's way, so the entry
        // it leaves keeps its scroll and the row it was left from (a click
        // with a modifier still opens a new tab)
        document.addEventListener('click', e => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            const a = e.target.closest && e.target.closest('a[href^="#/"]');
            if (!a || a.target === '_blank' || a.hasAttribute('data-action')) return;
            const r = this._routeParse(a.getAttribute('href'));
            if (!r) return;
            e.preventDefault();
            const href = a.getAttribute('href');
            if (href === location.hash) return;
            try {
                history.replaceState({ ...(history.state || {}), scrollY: window.scrollY,
                                       opened: r.parts[0] === 'devices' || r.parts[0] === 'installations' ? r.parts[1] || null : null },
                                     '', location.hash);
                history.pushState(null, '', href);
            } catch (err) { location.hash = href; return; }
            this._routeApply();
        });
        const r = this._routeParse(location.hash);
        if (r && r.parts.length) await this._routeApply({ initial: true });
        this._routeReady = true;
        try { history.replaceState(history.state, '', this._routeNow()); } catch (e) { /* */ }
        this._routeLast = location.hash;
    },
});

// follow the functions that change the view
(function () {
    const P = JanitzaMonitor.prototype;
    const follow = (name, push, when) => {
        const orig = P[name];
        if (typeof orig !== 'function') return;
        P[name] = function (...args) {
            // the scroll of the place being left, for Back to come home to
            if (!this._routePending && !this._routeApplying) this._routeLeave = { scrollY: window.scrollY };
            this._routePending = (this._routePending || 0) + 1;
            let out;
            try { out = orig.apply(this, args); } catch (e) { this._routePending -= 1; throw e; }
            const recorded = !when || when.apply(this, args);
            const asPush = typeof push === 'function' ? push.apply(this, args) : push;
            Promise.resolve(out).finally(() => {
                this._routePending -= 1;
                if (recorded) this._routeSoon(asPush);
                else if (!this._routePending) this._routeSoon(false);
            });
            return out;
        };
    };
    // a page opened only to host a device page records nothing on its own;
    // one the router opened (Back, a link) replaces, never truncating Forward
    follow('navigateTo', function (page, opts = {}) { return !opts._fromRoute; },
           function (page, opts = {}) { return !opts._opening; });
    follow('openDeviceDetail', true);
    follow('closeDeviceDetail', true);
    follow('openEndpointDetail', true);
    follow('_switchDeviceTab', false);
})();
