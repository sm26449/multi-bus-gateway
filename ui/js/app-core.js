/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
/**
 * Multi-Bus Gateway - Frontend Application
 */

// Utility: Debounce function
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

class JanitzaMonitor {
    constructor() {
        this.ws = null;
        this.currentValues = {};
        this.valueHistory = {};  // Pentru chart widget - stochează ultimele N valori
        this.allRegisters = {};
        this.selectedRegisters = [];
        this.currentPage = 'dashboard';
        // First-run prompt state: show a "connect your meter" modal only on a
        // genuinely unconfigured system (Modbus never connected), never on a
        // configured one having a temporary outage.
        this._everConnected = false;
        this._firstRunGraceOver = false;
        this._firstRunShown = false;
        this.registerSearchPage = 1;
        this.registersPerPage = 50;
        this.maxHistoryPoints = 60;  // 60 puncte pentru chart

        // Config page state
        this.configTab = 'all';
        this.configSearch = '';
        this.config = {};  // Server config (MQTT, InfluxDB status etc.)

        // Monitor page state
        this.monitorData = {};  // { address: { name, unit, color, data: [{time, value}], min, max } }
        this.monitorColors = ['#12a3b2', '#30d158', '#ff453a', '#ffd60a', '#bf5af2', '#ff9f0a'];
        this.monitorColorIndex = 0;
        this.monitorPaused = false;
        this.monitorMaxPoints = 120;  // 2 minutes at 1s interval
        this.monitorCanvas = null;
        this.monitorCtx = null;
        this.monitorSearch = '';

        // Monitor zoom/pan state
        this.monitorZoom = 1;
        this.monitorPanX = 0;  // Pan offset in pixels
        this.monitorIsDragging = false;
        this.monitorDragStart = { x: 0, y: 0 };
        this.monitorLastPanX = 0;

        // Monitor tooltip state
        this.monitorGraphParams = null;  // Store graph params for tooltip calculations

        // Performance optimizations
        this._flattenedRegistersCache = null;
        this._flattenedRegistersCacheKey = null;
        this._monitorRAFPending = false;

        // Debounced functions
        this._debouncedRenderRegisters = debounce(() => this.renderRegistersTable(), 200);
        this._debouncedRenderSelectedList = debounce(() => this.renderSelectedRegistersList(), 200);
        this._debouncedRenderMonitorCategories = debounce(() => this.renderMonitorCategories(), 150);

        // localStorage can THROW, not just return null (private windows,
        // blocked site data) — a throw here killed the whole app at boot
        const ls = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
        // Theme state
        this.theme = ls('mbg-theme') || 'auto';
        this.wasDisconnected = false;

        // Dashboard view state (cards or table)
        // Dashboard device (Phase B): null = primary; persisted per browser
        this.dashDevice = ls('mbg-dash-device') || null;
        this.dashValues = {};
        this.dashRegisters = [];

        // Threshold templates for auto-fill based on measurement type
        this.thresholdTemplates = {
            voltage_ln: {
                dangerLow: 200, warningLow: 210, warningHigh: 245, dangerHigh: 253,
                type: 'value', unit: 'V'
            },
            voltage_ll: {
                dangerLow: 346, warningLow: 363, warningHigh: 424, dangerHigh: 438,
                type: 'value', unit: 'V'
            },
            frequency: {
                dangerLow: 49.0, warningLow: 49.5, warningHigh: 50.5, dangerHigh: 51.0,
                type: 'value', unit: 'Hz'
            },
            power_factor: {
                dangerLow: 0.75, warningLow: 0.85, warningHigh: null, dangerHigh: null,
                type: 'value', unit: ''
            },
            thd: {
                dangerLow: null, warningLow: null, warningHigh: 5, dangerHigh: 8,
                type: 'value', unit: '%'
            }
            // no current/power template: a limit for those is a share of a
            // rating (the breaker, the inverter) this app cannot know — the
            // old 90/100 "percent" defaults were applied as raw amps/VA and
            // painted any load above 100 VA danger-red
        };

        this.init();
    }
}

// core helpers, i18n, theme, nav, websocket, boot init
Object.assign(JanitzaMonitor.prototype, {

    // ============ UI Helpers ============

    setButtonLoading(btn, loading, originalText = null) {
        if (loading) {
            btn.disabled = true;
            // keep the full markup — restoring via textContent stripped the
            // button's <i> icon permanently; and say "Saving…" in the UI's language
            btn.dataset.originalHtml = btn.innerHTML;
            btn.innerHTML = '<span class="btn-spinner"></span> '
                + this._esc(this.t('common.saving', 'Saving...'));
        } else {
            btn.disabled = false;
            if (originalText != null) btn.textContent = originalText;
            else if (btn.dataset.originalHtml != null) btn.innerHTML = btn.dataset.originalHtml;
            else btn.textContent = this.t('common.save', 'Save');
        }
    },

    // ============ Theme System ============

    initTheme() {
        // Apply saved theme immediately
        this.applyTheme(this.theme);

        // Listen for system preference changes
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
            if (this.theme === 'auto') {
                this.applyTheme('auto');
            }
        });

        // Setup toggle buttons
        this.setupThemeToggle();
    },

    applyTheme(theme) {
        this.theme = theme;
        localStorage.setItem('mbg-theme', theme);

        let effectiveTheme = theme;
        if (theme === 'auto') {
            effectiveTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }

        document.documentElement.setAttribute('data-theme', effectiveTheme);
        this.updateThemeIcons();
        this._applyDefaultColors();     // phase vars are theme-aware (IEC negru)
    },

    updateThemeIcons() {
        const toggle = document.getElementById('themeToggle');
        if (toggle) {
            const icon = toggle.querySelector('i');
            if (icon) {
                // Update icon based on current effective theme
                const effectiveTheme = this.theme === 'auto'
                    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
                    : this.theme;
                icon.className = effectiveTheme === 'dark' ? 'bi bi-moon-fill' : 'bi bi-sun-fill';
            }
        }
    },

    setupThemeToggle() {
        const toggle = document.getElementById('themeToggle');
        if (!toggle) return;

        const cycleTheme = () => {
            const cycle = { dark: 'light', light: 'auto', auto: 'dark' };
            this.applyTheme(cycle[this.theme]);
        };
        // Keyboard-operable control (2.1.1): the toggle is a div in markup,
        // so give it the button contract — focusable + Enter/Space.
        toggle.setAttribute('role', 'button');
        toggle.setAttribute('tabindex', '0');
        toggle.addEventListener('click', cycleTheme);
        toggle.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); cycleTheme(); }
        });
    },

    // ============ Connection Banner ============

    showConnectionBanner(type, text) {
        // The dedicated banner element was retired — surface connection state
        // via toasts (the statusbar dot tracks it too).
        this.showToast(type === 'connected' ? 'success' : 'error', text);
    },

    // ============ Error text hygiene ============

    _errMsg(x) {
        // Human-readable error text for toasts/panels: network failures get a
        // friendly line instead of "TypeError: Failed to fetch", FastAPI
        // validation payloads render their messages, objects never show as
        // [object Object], and a bare HTTP status reads as "HTTP 500".
        if (x instanceof TypeError) return this.t('err.network', 'Cannot reach the gateway.');
        if (x instanceof Error) return x.message || String(x);
        const d = (x && typeof x === 'object' && 'detail' in x) ? x.detail : x;
        if (d === null || d === undefined || d === '') return this.t('err.generic', 'Request failed.');
        if (Array.isArray(d)) return d.map(i => (i && (i.msg || i.error)) || JSON.stringify(i)).join('; ');
        if (typeof d === 'object') return d.error || d.msg || JSON.stringify(d);
        if (typeof d === 'number') return `HTTP ${d}`;
        return String(d);
    },

    // ============ Modal Helpers ============

    _modalFocusables(el) {
        return [...el.querySelectorAll(
            'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
            'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
            .filter(n => n.offsetParent !== null);
    },

    // opts.focus: the element that takes focus (default: the first focusable).
    openModal(modalId, opts = {}) {
        const modal = document.getElementById(modalId);
        if (!modal) return;
        // per modal, not one shared slot: a confirm opened over an editor
        // overwrote the editor's opener, and closing the editor stranded focus
        modal._returnFocus = document.activeElement;
        modal.classList.add('active');
        // Stacking: every .modal shares z-index 1000, so which of two OPEN
        // modals paints on top used to be DOM order — the enum builder
        // (declared early in index.html) rendered BEHIND the template editor
        // that opened it. The latest-opened modal must always win.
        this._modalZ = Math.max(this._modalZ || 1000, 1000) + 1;
        modal.style.zIndex = this._modalZ;
        document.body.classList.add('modal-open');
        // Dialog semantics on the content box (the backdrop is just chrome).
        const dialog = modal.querySelector('.modal-content') || modal;
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        dialog.tabIndex = -1;
        const head = dialog.querySelector('.modal-header h3, .modal-header h2, h3, h2');
        if (head) {
            if (!head.id) head.id = modalId + '-title';
            dialog.setAttribute('aria-labelledby', head.id);
        }
        // Move focus into the dialog (keyboard + screen-reader users land inside).
        setTimeout(() => {
            if (!modal.classList.contains('active')) return;
            const f = this._modalFocusables(modal);
            (opts.focus || f[0] || dialog).focus();
        }, 30);
        // Trap Tab within the dialog; Escape closes. stopPropagation: without
        // it the document-level shortcut handler saw the same Escape and
        // closed ANOTHER modal too — one press dismissed both the enum
        // builder and the template editor under it (audit 2026-10-01).
        const onKey = (e) => {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.closeModal(modalId); return; }
            if (e.key !== 'Tab') return;
            const f = this._modalFocusables(modal);
            if (!f.length) return;
            const first = f[0], last = f[f.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        };
        modal._trapHandler = onKey;
        modal.addEventListener('keydown', onKey);
    },

    closeModal(modalId) {
        // no id → the TOP-most open modal (highest z-index), not the first in
        // DOM order — with nested modals the old query closed the OUTER one
        const modal = modalId ? document.getElementById(modalId)
            : [...document.querySelectorAll('.modal.active')]
                .sort((a, b) => (+b.style.zIndex || 0) - (+a.style.zIndex || 0))[0];
        if (!modal) return;
        if (modal._keepOpen && modal._keepOpen()) return;
        modal.classList.remove('active');
        modal.style.zIndex = '';                 // back to the stylesheet default
        // nested modals (enum builder over the template editor): only unlock
        // the body scroll when the LAST open modal closes
        if (!document.querySelector('.modal.active')) {
            document.body.classList.remove('modal-open');
        }
        const dialog = modal.querySelector('.modal-content') || modal;
        dialog.removeAttribute('aria-modal');
        if (modal._trapHandler) {
            modal.removeEventListener('keydown', modal._trapHandler);
            modal._trapHandler = null;
        }
        // Return focus to whatever opened the modal (don't strand the caret).
        const ret = modal._returnFocus;
        modal._returnFocus = null;
        if (ret && typeof ret.focus === 'function') setTimeout(() => ret.focus(), 0);
        // whoever waits on this modal (app-dialogs) hears every way it closes:
        // ✕, Escape, Cancel — not only its own buttons
        const onClose = modal._onClose;
        modal._onClose = null;
        if (onClose) onClose();
    },

    // A time the gateway wrote as a naive ISO string (its own zone, no offset)
    // shown as written — a browser in another zone would otherwise move it by
    // the difference. Epoch numbers and strings with a zone are converted.
    _gatewayTime(v) {
        if (v === null || v === undefined || v === '') return '—';
        if (typeof v === 'number') return new Date(v * (v < 1e12 ? 1000 : 1)).toLocaleTimeString(this._loc());
        const s = String(v);
        const naive = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})(\.\d+)?$/.exec(s);
        if (naive) return naive[1];
        const d = new Date(s);
        return isNaN(d.getTime()) ? s : d.toLocaleTimeString(this._loc());
    },

    // ── First-run "connect your meter" prompt ──────────────────────────────
    // Shows once per session only when Modbus has never connected (fresh/
    // misconfigured deploy). Auto-closes the moment a read succeeds, and never
    // appears on a configured system having a temporary outage.
    _maybeFirstRun(modbus, devices = []) {
        // any device that reads counts: device #1 may be a spare that is off
        // while ten others run
        if ((modbus && modbus.last_success_ts)
                || (devices || []).some(d => d.connected || (d.successful_reads || 0) > 0)) {
            this._everConnected = true;
            this._closeFirstRun();
            return;
        }
        // setting a device up is an admin's job: never offered to anyone else
        if (this._authEnabled && this._role !== 'admin') return;
        if (this._everConnected || !this._firstRunGraceOver || this._firstRunShown) return;
        this._firstRunShown = true;
        this.openModal('firstRunModal');
    },

    _closeFirstRun() {
        const m = document.getElementById('firstRunModal');
        if (m && m.classList.contains('active')) this.closeModal('firstRunModal');
    },

    dismissFirstRun() { this.closeModal('firstRunModal'); },

    firstRunOpenSettings() {
        this.closeModal('firstRunModal');
        // Connection settings live on the device now (Devices → device → Edit)
        this.openDeviceDetail(this._primaryDeviceId()).then(() => this._switchDeviceTab('connection'));
    },

    // ============ Toast Notifications ============

    showToast(type, title, message, duration = 4000) {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        // Errors/warnings interrupt (assertive) via role=alert; success/info ride
        // the container's polite aria-live so a screen reader still announces them.
        if (type === 'error' || type === 'warning') toast.setAttribute('role', 'alert');

        const icons = {
            success: '&#10003;',
            error: '&#10007;',
            info: '&#8505;',
            warning: '&#9888;'
        };

        toast.innerHTML = `
            <span class="toast-icon" aria-hidden="true">${icons[type] || icons.info}</span>
            <div class="toast-content">
                <div class="toast-title">${this._esc(title)}</div>
                ${message ? `<div class="toast-message">${this._esc(message)}</div>` : ''}
            </div>
            <button class="toast-close" aria-label="${this._esc(this.t('common.dismiss', 'Dismiss'))}">&times;</button>
        `;

        toast.querySelector('.toast-close').addEventListener('click', () => {
            toast.classList.add('fade-out');
            setTimeout(() => toast.remove(), 300);
        });

        container.appendChild(toast);

        setTimeout(() => {
            if (toast.parentNode) {
                toast.classList.add('fade-out');
                setTimeout(() => toast.remove(), 300);
            }
        }, duration);
    },

    // ── i18n (languages/*.json; English is the always-loaded fallback base) ──
    async initI18n() {
        this._tEn = {};
        this._t = {};
        this._langs = [];
        this._defaultLang = 'en';
        try {
            const d = await (await fetch('/api/languages')).json();
            this._langs = d.languages || [];
            this._defaultLang = d.default || 'en';
        } catch (e) { /* no languages dir → UI stays English (hardcoded) */ }
        this._tEn = await this._fetchLang('en');
        const saved = localStorage.getItem('mbg-lang') || this._defaultLang;
        await this.setLanguage(saved, false);
        this._renderLangSelector();
    },

    async _fetchLang(code) {
        try { const r = await fetch(`/api/languages/${code}`); return r.ok ? await r.json() : {}; }
        catch (e) { return {}; }
    },

    // dates, times and numbers in the page's language, not the browser's:
    // en-GB keeps a 24-hour clock and day/month order
    _loc() {
        return ({ ro: 'ro-RO', en: 'en-GB' })[this._lang] || this._lang || 'en-GB';
    },

    t(key, fallback, params) {
        let s = (this._t && this._t[key]) || fallback || key;
        if (params) s = s.replace(/\{(\w+)\}/g, (m, name) => name in params ? String(params[name]) : m);
        return s;
    },

    // a count said in the language's own plural: `${key}.${category}` (one,
    // few, other… from Intl.PluralRules), else `${key}`; {n} is the count
    tn(key, n, fallback, params = {}) {
        let cat = 'other';
        try { cat = new Intl.PluralRules(this._lang || 'en').select(n); } catch (e) { /* old browser */ }
        const k = (this._t && this._t[`${key}.${cat}`]) ? `${key}.${cat}`
            : (this._t && this._t[`${key}.other`]) ? `${key}.other` : key;
        return this.t(k, fallback, { ...params, n });
    },

    async setLanguage(code, persist = true) {
        const sel = (code && code !== 'en') ? await this._fetchLang(code) : this._tEn;
        this._t = { ...this._tEn, ...sel };          // selected overrides the English base
        this._lang = code;
        if (persist) localStorage.setItem('mbg-lang', code);
        document.documentElement.lang = code;
        this.applyTranslations();
        this._renderLangSelector();
        // Re-render the current page only on a user-initiated switch (persist), so
        // dynamic t() strings update without double-rendering during initial load.
        if (persist && this.currentPage) this.navigateTo(this.currentPage);
    },

    // a dictionary text with markup → HTML where only b, strong, em, code and
    // br are tags; everything else is escaped text
    _i18nMarkup(text) {
        return this._esc(String(text ?? ''))
            .replace(/&lt;(\/?)(b|strong|em|code)&gt;/g, '<$1$2>')
            .replace(/&lt;br\s*\/?&gt;/g, '<br>');
    },

    applyTranslations() {
        document.querySelectorAll('[data-i18n]').forEach(el => {
            if (el.dataset.i18nOrig === undefined) el.dataset.i18nOrig = el.textContent;
            el.textContent = this.t(el.getAttribute('data-i18n'), el.dataset.i18nOrig);
        });
        // a text with a few words marked (bold, code): opt-in per element, and
        // only those tags survive — every other < is shown, never parsed
        document.querySelectorAll('[data-i18n-html]').forEach(el => {
            if (el.dataset.i18nOrigHtml === undefined) el.dataset.i18nOrigHtml = el.innerHTML;
            el.innerHTML = this._i18nMarkup(this.t(el.getAttribute('data-i18n-html'), el.dataset.i18nOrigHtml));
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            if (el.dataset.i18nOrigPh === undefined) el.dataset.i18nOrigPh = el.getAttribute('placeholder') || '';
            el.setAttribute('placeholder', this.t(el.getAttribute('data-i18n-placeholder'), el.dataset.i18nOrigPh));
        });
        document.querySelectorAll('[data-i18n-title]').forEach(el => {
            if (el.dataset.i18nOrigTitle === undefined) el.dataset.i18nOrigTitle = el.getAttribute('title') || '';
            el.setAttribute('title', this.t(el.getAttribute('data-i18n-title'), el.dataset.i18nOrigTitle));
        });
        // aria-labels are UI text too — without this, every labelled control
        // stayed English for a screen-reader user who switched the language
        document.querySelectorAll('[data-i18n-aria]').forEach(el => {
            if (el.dataset.i18nOrigAria === undefined) el.dataset.i18nOrigAria = el.getAttribute('aria-label') || '';
            el.setAttribute('aria-label', this.t(el.getAttribute('data-i18n-aria'), el.dataset.i18nOrigAria));
        });
    },

    // Secondary tab bars (.config-main-tabs): tab semantics + selection state
    // synced from .active — the bars were plain buttons whose active state was
    // color-only. Call after every toggle; idempotent.
    // One tab stop per bar (roving tabindex): the selected tab is reached
    // with Tab, ←/→/Home/End move between the bar's shown tabs, Enter or
    // Space opens one (manual activation — some tabs embed a heavy page).
    // A tab names its panel (aria-controls) only when that panel exists.
    _tabAria(sel) {
        const bar = typeof sel === 'string' ? document.querySelector(sel) : sel;
        if (!bar) return;
        bar.setAttribute('role', 'tablist');
        const tabs = [...bar.querySelectorAll('[role=tab], .config-main-tab')];
        const shown = tabs.filter(t => !t.hidden);
        const sel1 = shown.find(t => t.classList.contains('active')) || shown[0];
        tabs.forEach(t => {
            t.setAttribute('role', 'tab');
            const on = t.classList.contains('active');
            t.setAttribute('aria-selected', String(on));
            t.tabIndex = t === sel1 ? 0 : -1;
            const pid = t.dataset.panelId;
            if (pid && document.getElementById(pid)) t.setAttribute('aria-controls', pid);
        });
        if (bar._keys) return;
        bar._keys = true;
        bar.addEventListener('keydown', e => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
            const its = [...bar.querySelectorAll('[role=tab]')].filter(t => !t.hidden);
            const i = its.indexOf(document.activeElement);
            if (i < 0 || !its.length) return;
            e.preventDefault();
            const j = e.key === 'Home' ? 0 : e.key === 'End' ? its.length - 1
                : (i + (e.key === 'ArrowRight' ? 1 : -1) + its.length) % its.length;
            its.forEach(t => { t.tabIndex = t === its[j] ? 0 : -1; });
            its[j].focus();
            if (its[j].scrollIntoView) its[j].scrollIntoView({ block: 'nearest', inline: 'nearest' });
        });
    },

    // ── one actions menu (⋯) ────────────────────────────────────────────────
    // A button (aria-haspopup="menu", aria-controls=<the menu's id>) and its
    // role=menu list of role=menuitem buttons. ↑/↓/Home/End move, Esc and a
    // click elsewhere close it; an item closes the menu and gives the focus
    // back to the button BEFORE it acts, so a dialog it opens returns there.
    // One document listener serves every menu on the page.
    _menuToggle(btn, open) {
        if (typeof btn === 'string') btn = document.getElementById(btn);
        const menu = btn && document.getElementById(btn.getAttribute('aria-controls'));
        if (!menu) return;
        this._menuWire();
        const willOpen = open === undefined ? menu.hidden : !!open;
        if (willOpen) document.querySelectorAll('.mbg-menu:not([hidden])').forEach(m => {
            if (m !== menu) this._menuClose(m);
        });
        menu.hidden = !willOpen;
        btn.setAttribute('aria-expanded', String(willOpen));
        if (willOpen) this._menuItems(menu)[0]?.focus();
    },

    _menuItems(menu) {
        return [...menu.querySelectorAll('[role=menuitem]')].filter(x => !x.hidden && x.offsetParent !== null);
    },

    _menuClose(menu, focusBtn = false) {
        if (!menu || menu.hidden) return;
        menu.hidden = true;
        const btn = document.querySelector(`[aria-controls="${CSS.escape(menu.id)}"]`);
        if (btn) { btn.setAttribute('aria-expanded', 'false'); if (focusBtn) btn.focus(); }
    },

    _menuWire() {
        if (this._menuWired) return;
        this._menuWired = true;
        // capture: the item closes its menu and hands the focus to its button
        // before the item's own action runs
        document.addEventListener('click', e => {
            const item = e.target.closest && e.target.closest('.mbg-menu [role=menuitem]');
            if (item) { this._menuClose(item.closest('.mbg-menu'), true); return; }
            document.querySelectorAll('.mbg-menu:not([hidden])').forEach(m => {
                const btn = document.querySelector(`[aria-controls="${CSS.escape(m.id)}"]`);
                if (!m.contains(e.target) && !(btn && btn.contains(e.target))) this._menuClose(m);
            });
        }, true);
        document.addEventListener('keydown', e => {
            // Esc on the ⋯ button of an open menu closes it too
            const opener = e.key === 'Escape' && e.target.closest && e.target.closest('[aria-haspopup=menu][aria-expanded=true]');
            if (opener) {
                const m = document.getElementById(opener.getAttribute('aria-controls'));
                if (m && !m.hidden) { e.preventDefault(); e.stopPropagation(); this._menuClose(m, true); return; }
            }
            const menu = e.target.closest && e.target.closest('.mbg-menu');
            if (!menu || menu.hidden) return;
            const its = this._menuItems(menu);
            const i = its.indexOf(document.activeElement);
            let j = null;
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this._menuClose(menu, true); return; }
            if (e.key === 'Tab') { this._menuClose(menu); return; }
            if (e.key === 'ArrowDown') j = (i + 1) % its.length;
            else if (e.key === 'ArrowUp') j = (i - 1 + its.length) % its.length;
            else if (e.key === 'Home') j = 0;
            else if (e.key === 'End') j = its.length - 1;
            if (j == null || !its.length) return;
            e.preventDefault();
            its[j].focus();
        }, true);
    },

    _renderLangSelector() {
        const el = document.getElementById('langSelect');
        if (!el) return;
        if (!this._langs.length) { el.style.display = 'none'; return; }
        el.innerHTML = this._langs.map(l =>
            `<option value="${this._esc(l.code)}" ${l.code === this._lang ? 'selected' : ''}>${this._esc(l.flag || '')} ${this._esc(l.nativeName || l.name)}</option>`).join('');
        if (!el._wired) { el._wired = true; el.addEventListener('change', () => this.setLanguage(el.value)); }
    },

    async init() {
        this.installApiAuth();   // wrap fetch so writes carry the optional API key
        // Initialize theme FIRST to prevent flash
        this.initTheme();
        await this.initI18n();   // load languages + apply the saved/default one

        // Login gate: if auth is enabled and we're not authenticated, show the
        // login overlay and stop here — the rest of init runs after login.
        if (!(await this._checkAuth())) {
            this._showLogin();
            return;
        }
        // Authenticated (or auth off): reveal the logout button when auth is on
        if (this._authEnabled) {
            const lb = document.getElementById('logoutBtn');
            if (lb) {
                lb.classList.add('visible');
                if (this._role === 'viewer') lb.title = 'Viewer (read-only) — log out';
            }
        }
        // After a grace window, a still-never-connected Modbus means "unconfigured".
        setTimeout(() => { this._firstRunGraceOver = true; }, 10000);

        // Setup navigation
        this.setupNavigation();

        // Setup event listeners
        this.setupEventListeners();
        this._wireDashboardClicks();

        // Connect WebSocket
        this.connectWebSocket();

        // Load initial data
        await this.loadConfig();
        // Default widget colors (phase convention + categories) — needed at
        // boot, not just on the Settings page; failure keeps built-ins.
        fetch('/api/config/general').then(r => r.ok ? r.json() : null).then(g => {
            if (g) { this._defaultColors = g.default_colors || {}; this._applyDefaultColors(); }
        }).catch(() => {});
        await this.loadStatus();
        await this.loadAllRegisters();
        await this.loadSelectedRegisters();
        this._loadCanonicalFields();   // canonical dictionary for register-editor guidance
        // dashboard device dimension: restore the persisted selection (its
        // registers + value snapshot), then render
        await this._setDashDevice(this.dashDevice || null);
        // fleet overview: the dashboard's default face with 2+ devices
        await this.initFleet();
        this.updateDashboard();
        this.renderDashDeviceChips();

        // Start status polling (stopped by _sessionLost)
        this._statusTimer = setInterval(() => this.loadStatus(), 5000);
        // the address says where to go (a link, a reload); from now on it follows the view
        if (this._routeStart) await this._routeStart();
    },

    async loadConfig() {
        try {
            const response = await fetch('/api/config');
            if (!response.ok) throw new Error('HTTP ' + response.status);
            this.config = await response.json();
        } catch (error) {
            console.error('Failed to load config:', error);
        }
    },

    setupNavigation() {
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', (e) => {
                e.preventDefault();
                const page = tab.dataset.page;
                // a click on the tab means "take me to that page's start" —
                // Devices goes back to the list even with a device open
                this.navigateTo(page, { fromNav: true });
            });
        });
    },

    navigateTo(page, opts = {}) {
        const prevPage = this.currentPage;
        // unsaved security settings: ask before leaving them behind, and put
        // the stored values back when the answer is "leave"
        // (navigateTo is synchronous: it stops here, and the answer "leave"
        // navigates again with _leave set)
        if (prevPage === 'config' && page !== 'config' && this._secDirty && !opts._leave) {
            this.confirmAction({
                title: this.t('security.leaveTitle', 'Leave without saving?'),
                message: this.t('security.leaveUnsaved', 'The security settings have unsaved changes. Leaving puts the saved values back.'),
                confirmLabel: this.t('security.leave', 'Leave'),
                cancelLabel: this.t('security.stay', 'Stay'),
            }).then(ok => {
                if (!ok || this.currentPage !== 'config') return;
                if (this._secDiscard) this._secDiscard();
                this.navigateTo(page, { ...opts, _leave: true });
            });
            return;
        }
        // Update nav tabs (class + ARIA selected state for the tablist)
        document.querySelectorAll('.nav-tab').forEach(t => {
            t.classList.remove('active');
            if (t.hasAttribute('role')) t.setAttribute('aria-selected', 'false');
        });
        const activeTab = document.querySelector(`.nav-tab[data-page="${page}"]`);
        if (activeTab) {
            activeTab.classList.add('active');
            if (activeTab.hasAttribute('role')) activeTab.setAttribute('aria-selected', 'true');
        }

        // leaving Devices: an open device page stops its log poll and sends
        // the pages it embedded (Monitor, History…) home — before the pages
        // are switched, so the one asked for is shown where it lives
        if (page !== 'devices' && this._leaveDeviceDetail) this._leaveDeviceDetail();

        // Update pages
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.getElementById(`${page}Page`)?.classList.add('active');

        this.currentPage = page;
        if (this.updatePollGroupsStatus) this.updatePollGroupsStatus();   // the footer follows the page

        // stop any virtual-meter observability polling when leaving the page
        if (page !== 'vmeters' && this._stopVmPolls) this._stopVmPolls();
        if (page !== 'rules' && this._stopRulesPolls) this._stopRulesPolls();
        // stop the Monitor's non-primary value poll when leaving Monitor
        if (page !== 'monitor' && this._stopMonitorPoll) this._stopMonitorPoll();
        // stop the Status page auto-refresh when leaving it
        if (page !== 'status' && this._stopStatusPoll) this._stopStatusPoll();
        // stop the bus-monitor live poll when leaving Diagnostics
        if (page !== 'diagnostics' && this._stopDiagPoll) this._stopDiagPoll();

        // The register-editing context (device selector) only follows the
        // registers/config pages; anywhere else snaps back to device #1 so the
        // dashboard/monitor never mix another device's register set.
        if (page !== 'registers' && page !== 'config' && page !== 'devices') this._maybeResetRegDevice();

        // Page-specific init
        if (page === 'devices') {
            // Keep an open detail/register editor alive across re-navigations
            // (language switch re-runs navigateTo on the current page).
            const detailOpen = document.getElementById('deviceDetailView')?.style.display !== 'none'
                            && document.getElementById('deviceDetailView')?.innerHTML;
            const regOpen = document.getElementById('deviceRegistersView')?.style.display !== 'none';
            if (opts._opening) {
                // the caller shows a device or an installation itself (and the
                // router draws the list it restores): nothing drawn here
            } else if (opts.fromNav || (!detailOpen && !regOpen)) {
                this._showDevicesList();
                this.renderDevicesList();
            } else if (detailOpen && prevPage !== 'devices' && this._devDetail && !opts._opening) {
                // back on a device page left open: its tab starts again
                // (its embedded page and log poll were stopped on the way out)
                const tab = document.querySelector('#deviceWsTabs .config-main-tab.active[data-dtab]');
                if (tab) this._switchDeviceTab(tab.dataset.dtab);
                const rv = this._devDetail.entry?.read_via || [];
                this._footerOverride = rv.length ? rv : null;
            }
        } else if (page === 'config') {
            if (prevPage === 'config' && this._secDirty) {
                // already here with unsaved security edits (a language switch,
                // a link on the page): keep them, redraw what is translated
                if (this._secPaint) this._secPaint();
            } else {
                this.loadSettingsConfig();
                // a re-render (a language switch) stays on the sub-tab it was on
                this.switchConfigTab(prevPage === 'config' && this._cfgTab ? this._cfgTab : 'mqtt');
            }
        } else if (page === 'monitor') {
            this.initMonitorPage();
        } else if (page === 'history') {
            this.initHistoryPage();
        } else if (page === 'energy') {
            this.initEnergyPage();
        } else if (page === 'vmeters') {
            this.renderVirtualMeters();
        } else if (page === 'rules') {
            this.renderRulesPage();
        } else if (page === 'templates') {
            this.renderTemplateManager();
        } else if (page === 'status') {
            this.initStatusPage();
        } else if (page === 'diagnostics') {
            this.initDiagnosticsPage();
        }
    },

    // an icon-only button's name: what it does to which thing, for a screen
    // reader (unique in a list of rows), and the same words shown on hover
    // and on keyboard focus (data-tip; a title never shows on focus or touch)
    _ib(action, name) {
        const a = String(action || '');
        const full = name ? `${a} ${name}` : a;
        return `aria-label="${this._esc(full)}" data-tip="${this._esc(a)}"`;
    },

    _fmtInterval(s) {
        s = Number(s);
        return (isFinite(s) && s < 1) ? `${Math.round(s * 1000)}ms` : `${s}s`;
    },

    _hexA(hex, a) {
        hex = String(hex).trim().replace('#', '');
        if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
        const n = parseInt(hex || '2f81f7', 16);
        return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    },

    // ── Default widget colors (Settings → General) ─────────────────────────
    // Phase conventions: hex triplets per theme. 'iec' maps the RO/EU conductor
    // colors (maro/negru/gri) to screen-legible tints — true black is invisible
    // on dark, so 'negru' renders near-white there (standard SCADA practice).
    _PHASE_CONVENTIONS: {
        distinct: { light: ['#3b82f6', '#12a3b2', '#8b5cf6'],
                    dark:  ['#3b82f6', '#12a3b2', '#8b5cf6'] },
        iec:      { light: ['#8a5a2b', '#3f4750', '#8a94a0'],
                    dark:  ['#c08a52', '#e8e8ea', '#9aa4af'] },
        rst:      { light: ['#c0504a', '#b58414', '#3b82f6'],
                    dark:  ['#d4635d', '#d29922', '#3b82f6'] },
    },
    _CATEGORY_DEFAULTS: { temperature: '#f97316', humidity: '#06b6d4',
                          power: '#12a3b2', energy: '#d29922' },

    // Set --phase-l1..3 from the configured convention + effective theme.
    // Called after config load and on every theme switch.
    _applyDefaultColors() {
        const dc = this._defaultColors || {};
        const conv = dc.phase_convention || 'distinct';
        const theme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        const triplet = conv === 'custom' && (dc.phase_custom || []).length === 3
            ? dc.phase_custom
            : (this._PHASE_CONVENTIONS[conv] || this._PHASE_CONVENTIONS.distinct)[theme];
        triplet.forEach((c, i) =>
            document.documentElement.style.setProperty(`--phase-l${i + 1}`, c));
    },

    // The default color for a register: phase identity when the name/label
    // says L1/L2/L3 (or [0]/[1]/[2]) on a per-phase quantity, else the
    // category hue by unit, else the accent. Returns var(--phase-lN) for
    // phases so theme/convention switches recolor live (SVG accepts var()).
    _defaultColorFor(reg) {
        const name = `${reg.name || ''} ${reg.label || ''}`;
        const unit = (reg.unit || '').toLowerCase();
        let phase = null;
        const m = name.match(/\[([012])\]/);
        if (m) phase = Number(m[1]) + 1;
        else { const lm = name.match(/\bL([123])\b/i); if (lm) phase = Number(lm[1]); }
        if (phase && phase <= 3 && ['v', 'a', 'w', 'va', 'var', 'kw'].includes(unit)) {
            return `var(--phase-l${phase})`;
        }
        const cats = (this._defaultColors || {}).categories || {};
        const pick = (k) => cats[k] || this._CATEGORY_DEFAULTS[k];
        if (unit === '°c' || unit === 'c' || unit === '°f') return pick('temperature');
        if (unit === '%' && /hum|umid/i.test(name)) return pick('humidity');
        if (unit === 'w' || unit === 'kw') return pick('power');
        if (['wh', 'kwh', 'mwh', 'varh', 'kvarh', 'vah', 'kvah'].includes(unit)) return pick('energy');
        return 'var(--accent)';
    },

    // Resolve var(--x) to a concrete hex/rgb (color <input> needs a literal).
    _resolveCssColor(c) {
        if (!c || !c.startsWith('var(')) return c;
        const name = c.slice(4, -1).trim();
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#3b82f6';
    },

    // Locale-aware number for display: thousands grouping + fixed fraction
    // digits (54840 → 54,840 / -45017.62 → -45,017.62). One formatter for the
    // whole UI so KPIs, cards and tables never disagree on formatting.
    // one way to show a measured value, everywhere: the dashboard's unit
    // scaling (W → kW, Wh → kWh …), locale separators, and seconds as a
    // duration. Returns {text, unit}; text is not escaped (numbers only,
    // or the value as given when it is not a number — escape that).
    _fmtValue(value, unit) {
        if (typeof value !== 'number' || !isFinite(value)) return { text: value == null ? '—' : String(value), unit: unit || '', raw: true };
        if (unit === 's' && Math.abs(value) >= 60) {
            const sec = Math.floor(Math.abs(value)), d = Math.floor(sec / 86400),
                h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
            const txt = d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min ${sec % 60} s`;
            return { text: (value < 0 ? '−' : '') + txt, unit: '' };
        }
        const f = this.formatValueWithUnit(value, unit || '');
        return { text: this._fmtNum(f.value, f.decimals), unit: f.unit || '' };
    },

    // one empty state for every page: an icon, a short title, one sentence
    // and (optionally) the action that fills it. Strings are escaped here;
    // actionHtml is trusted markup built by the caller.
    _emptyState({ icon = 'bi-inbox', title = '', body = '', actionHtml = '' } = {}) {
        return `<div class="empty-state compact" role="status">
            <i aria-hidden="true" class="bi ${this._esc(icon)}"></i>
            ${title ? `<h3>${this._esc(title)}</h3>` : ''}
            ${body ? `<p>${this._esc(body)}</p>` : ''}
            ${actionHtml || ''}</div>`;
    },

    _fmtNum(value, decimals = 2) {
        if (typeof value !== 'number' || isNaN(value)) return '--';
        // an integer is an integer: 'Packs online 8.00' and 'Alarms 0.00'
        // read as broken, not precise (counts, enum codes, whole mV deltas)
        const d = Number.isInteger(value) ? 0 : decimals;
        return value.toLocaleString(this._loc(), {
            minimumFractionDigits: d, maximumFractionDigits: d });
    },

    _esc(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/'/g, '&#39;').replace(/`/g, '&#96;');
    },

    _dur(s) {
        s = Math.max(0, Math.floor(s || 0));
        if (s < 60) return s + 's';
        if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
        if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
        return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
    },

    // Delegated actions: elements rendered with a data-action attribute dispatch
    // to app.<method>(...) — the page carries no inline event handlers at all.
    // Motivation (U1): _esc() is an HTML-context escaper, but an inline handler
    // is a JS-string-inside-attribute context — the HTML parser decodes &#39;
    // back to ' BEFORE the JS engine parses the handler, so a value containing a
    // quote breaks out of the JS string. With data-* + delegation the value only
    // ever lives in attribute context, where _esc IS the right escaper.
    // Conventions:
    //   data-args='["a",1]'  JSON array of arguments (attribute-escaped)
    //   data-with-el         append the clicked element as the last argument
    //   data-guard=".sel"    ignore clicks landing inside a matching descendant
    //                        (row-click vs. its action-buttons cell)
    // Every UI action is declared in markup as data-action="<method>" (plus
    // data-args JSON, data-with-el, data-with-value, data-guard and
    // data-on="click|change|input") and dispatched from here. The page carries
    // no inline event handlers, so the CSP can forbid inline script entirely
    // (script-src 'self') and an injected attribute can never execute.
    _wireActionDelegation() {
        const find = (target, type) => {
            let el = target && target.closest ? target.closest('[data-action]') : null;
            // an <input data-on="change"> inside a clickable row: the click still
            // belongs to the row, so keep walking up past elements of another type
            while (el && (el.dataset.on || 'click') !== type) {
                el = el.parentElement ? el.parentElement.closest('[data-action]') : null;
            }
            return el;
        };
        const dispatch = (ev, type) => {
            const el = find(ev.target, type);
            if (!el) return;
            const guard = el.dataset.guard;
            if (guard && ev.target.closest(guard) !== el && ev.target.closest(guard)) return;
            if (type === 'click' && el.tagName === 'A') ev.preventDefault();   // href="#" links
            const fn = this[el.dataset.action];
            if (typeof fn !== 'function') {
                console.warn('data-action: unknown method', el.dataset.action);
                return;
            }
            let args = [];
            if (el.dataset.args) {
                try { args = JSON.parse(el.dataset.args); }
                catch (e) { console.error('data-action: bad data-args', el.dataset.args); return; }
            }
            if ('withEl' in el.dataset) args.push(el);
            if ('withValue' in el.dataset) args.push(el.value);
            fn.apply(this, args);
        };
        document.addEventListener('click', (ev) => dispatch(ev, 'click'));
        document.addEventListener('change', (ev) => dispatch(ev, 'change'));
        document.addEventListener('input', (ev) => dispatch(ev, 'input'));
        // a formula or a topic pasted with characters nobody sees: said at once,
        // under the field (the server refuses to save them anyway)
        document.addEventListener('input', (ev) => this._pasteCheck(ev.target));
        // Enter on non-<button> actionable elements (div[role=button] rows) —
        // real buttons already synthesize a click on Enter natively.
        document.addEventListener('keydown', (ev) => {
            if (ev.key !== 'Enter') return;
            const el = ev.target.closest('[data-action]');
            if (el && 'keyEnter' in el.dataset) dispatch(ev, 'click');
        });
    },

    // Build the data-action attribute string for a template literal:
    //   `<button ${this._act('deleteDevice', [d.id])}>` — args JSON-encoded and
    // attribute-escaped in one place so call sites can't get the escaping wrong.
    // opts: el (append the element), value (append el.value), guard (selector
    // whose nested matches don't count), on ('change' | 'input' instead of click).
    // Modbus transports on a serial port of this host (RTU or ASCII framing)
    _isSerialProto(p) { return p === 'rtu' || p === 'ascii'; },

    // A saved device's connection, as a PUT/test payload sends it back: a
    // bridged device keeps only its bridge reference; serial, network and
    // HTTP keep their own fields. null for MQTT (its password never reaches
    // the browser) — the caller then must not resend the connection.
    _connPayloadOf(dev) {
        if (!dev) return null;
        const c = dev.connection || {};
        const p = c.protocol || dev.protocol || 'tcp';
        if (dev.bridge) return { ...(p === 'rtu_tap' ? { protocol: 'rtu_tap' } : {}), bridge: dev.bridge,
                                 bridge_port: dev.bridge_port, unit_id: c.unit_id ?? dev.unit_id, timeout: c.timeout ?? 3 };
        if (p === 'http') return { protocol: 'http', url: c.url || dev.http_url || '' };
        if (p === 'mqtt') return null;
        if (this._isSerialProto(p) || p === 'rtu_tap')
            return { protocol: p, serial_port: c.serial_port || dev.serial?.serial_port, baudrate: c.baudrate || dev.serial?.baudrate,
                     parity: c.parity || dev.serial?.parity, stopbits: c.stopbits || dev.serial?.stopbits, unit_id: c.unit_id ?? dev.unit_id };
        return { protocol: p, host: c.host || dev.host, port: c.port || dev.port, unit_id: c.unit_id ?? dev.unit_id, timeout: c.timeout ?? 3 };
    },

    // What a template tells its users (a limitation, a model it does not fit,
    // a step to take first) — shown where the template is picked and on every
    // device made from it. `fields` names the rows/commands a note is about.
    // notes: the template's own notes. folded: one line "N notes" that opens
    // them (the device page — they are read once, not on every visit)
    _tplNotesHtml(notes, { folded = false } = {}) {
        const list = (notes || []).filter(n => n && n.text);
        if (!list.length) return '';
        const body = this._tplNotesList(list);
        if (!folded) return body;
        const warn = list.some(n => n.level === 'warning');
        return `<details class="tpl-notes-fold${warn ? ' warn' : ''}">
            <summary><i aria-hidden="true" class="bi ${warn ? 'bi-exclamation-triangle' : 'bi-info-circle'}"></i>
            ${this._esc(this.tn('tplNotes.count', list.length, '{n} notes about this template'))}</summary>${body}</details>`;
    },

    _tplNotesList(list) {
        return `<div class="tpl-notes">${list.map(n => {
            const warn = n.level === 'warning';
            const fields = (n.fields || []).map(f => `<code>${this._esc(f)}</code>`).join(' ');
            const link = /^https?:\/\//.test(n.link || '')
                ? ` <a href="${this._esc(n.link)}" target="_blank" rel="noopener">${this.t('common.moreInfo', 'More')} <i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></a>` : '';
            return `<div class="tpl-note ${warn ? 'warn' : ''}" role="note">
                <i aria-hidden="true" class="bi ${warn ? 'bi-exclamation-triangle' : 'bi-info-circle'}"></i>
                <div><span class="tpl-note-text">${this._esc(n.text)}</span>${link}${fields ? `<div class="tpl-note-fields">${fields}</div>` : ''}</div>
            </div>`;
        }).join('')}</div>`;
    },

    // The warning notes that name `field` (a row or a command) — one line.
    _tplNoteFor(notes, field) {
        return (notes || []).filter(n => n && n.level === 'warning' && (n.fields || []).includes(field))
            .map(n => n.text).join(' · ');
    },

    // A register's identity: its address IN ITS TABLE. coil 0, holding 0 and
    // input 0 are three registers; the server's store keys each by address +
    // the table's offset (the API sends it as `key`). The address itself is
    // only for display.
    _rk(reg) {
        if (!reg) return null;
        if (reg.key != null) return Number(reg.key);
        const off = { input: 100000, coil: 200000, discrete: 300000 }[String(reg.register_type || 'holding').toLowerCase()] || 0;
        return Number(reg.address) + off;
    },

    // ── what a copy/paste brings (mirrors multibus/text_check.py) ──────────
    _PASTE_FORMULA_IDS: ['calcExpr', 'advExpr', 'rlSignal', 'rlWhen', 'rlUntil', 'rlSchedWhen', 'rlTrimSignal'],
    _PASTE_TOPIC_IDS: ['ddvMqttTopic', 'devWizMqttTopic', 'ddvKaTopic', 'tplKaTopic', 'tplAvTopic'],
    _PASTE_INSTEAD: {
        0x2212: '-', 0x2010: '-', 0x2011: '-', 0x2012: '-', 0x2013: '-', 0x2014: '-',
        0x00A0: ' ', 0x202F: ' ', 0x2009: ' ', 0x2007: ' ', 0x3000: ' ',
        0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201C: '"', 0x201D: '"', 0x201E: '"', 0x00AB: '"', 0x00BB: '"',
        0x00D7: '*', 0x22C5: '*', 0x00B7: '*', 0x00F7: '/', 0x2215: '/',
        0x2264: '<=', 0x2265: '>=', 0x2260: '!=', 0xFF08: '(', 0xFF09: ')', 0xFF0C: ',',
    },
    _PASTE_INVISIBLE: [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x200E, 0x200F,
                       0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069],

    _pasteCharIssue(ch, pos) {
        const cp = ch.codePointAt(0);
        const code = 'U+' + cp.toString(16).toUpperCase().padStart(4, '0');
        if (this._PASTE_INVISIBLE.includes(cp) || cp < 0x20 || (cp >= 0x7F && cp < 0xA0))
            return this.t('paste.invisible', 'An invisible character at position {pos} ({code}) — delete it (it usually comes with a copy/paste).', { pos, code });
        if (cp in this._PASTE_INSTEAD) {
            const fix = this._PASTE_INSTEAD[cp] === ' ' ? this.t('paste.space', 'a normal space') : `“${this._PASTE_INSTEAD[cp]}”`;
            return this.t('paste.instead', '“{ch}” at position {pos} ({code}) is not allowed — type {fix} instead.', { ch, pos, code, fix });
        }
        if (/\p{L}/u.test(ch))
            return this.t('paste.letter', '“{ch}” at position {pos} ({code}) is not a plain letter — names use a–z, 0–9 and _ (a look-alike or a diacritic names a value that does not exist).', { ch, pos, code });
        return this.t('paste.other', '“{ch}” at position {pos} ({code}) is not allowed here.', { ch, pos, code });
    },

    // the first character a formula must not carry, or ''
    _formulaIssue(text) {
        let pos = 0;
        for (const ch of String(text || '')) {
            pos++;
            if (ch === '\t' || ch === '\n' || ch === '\r' || (ch >= ' ' && ch <= '~')) continue;
            return this._pasteCharIssue(ch, pos);
        }
        return '';
    },

    // a topic may carry any printable text, never an invisible character, a
    // look-alike space or a space at either end
    _topicIssue(text) {
        const s = String(text || '');
        let pos = 0;
        for (const ch of s) {
            pos++;
            const cp = ch.codePointAt(0);
            if (this._PASTE_INVISIBLE.includes(cp) || cp < 0x20 || (cp >= 0x7F && cp < 0xA0)
                    || (/\s/u.test(ch) && ch !== ' ')) return this._pasteCharIssue(ch, pos);
        }
        if (s !== s.replace(/^ +| +$/g, '')) return this.t('paste.edgeSpace', 'It starts or ends with a space — remove it.');
        return '';
    },

    _pasteCheck(el) {
        if (!el || !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) return;
        const formula = this._PASTE_FORMULA_IDS.includes(el.id);
        const topic = this._PASTE_TOPIC_IDS.includes(el.id) || ['topic', 'write_topic'].includes(el.dataset.f);
        if (!formula && !topic) return;
        const msg = formula ? this._formulaIssue(el.value) : this._topicIssue(el.value);
        const host = el.closest('.secret-field') || el;
        let box = host.nextElementSibling;
        if (!box || !box.classList.contains('paste-issue')) box = null;
        if (!msg) {
            el.removeAttribute('aria-invalid');
            box?.remove();
            return;
        }
        el.setAttribute('aria-invalid', 'true');
        if (!box) {
            box = document.createElement('div');
            box.className = 'field-error paste-issue';
            box.setAttribute('role', 'alert');
            host.insertAdjacentElement('afterend', box);
        }
        box.textContent = msg;
    },

    // A field whose value should not sit on the screen (a password, a topic
    // carrying a site id): shown as •••• with an eye that reveals it.
    // a masked input with a Show/Hide toggle; `what` names it for a screen
    // reader ("Show topic", not a bare "Show" beside every masked field)
    _secretInput(id, value, attrs = '', what = '') {
        const show = this._esc(what ? this.t('common.showWhat', 'Show {what}', { what }) : this.t('common.show', 'Show'));
        return `<div class="secret-field" style="display:flex;gap:4px;align-items:center;">
            <input type="password" id="${id}" class="input" value="${this._esc(value ?? '')}" ${attrs}
                autocomplete="new-password" data-lpignore="true" data-1p-ignore spellcheck="false" style="flex:1;min-width:0;">
            <button type="button" class="btn btn-ghost btn-sm" ${this._act('toggleSecret', [id], { el: true })}
                ${what ? `data-secret-name="${this._esc(what)}"` : ''} title="${show}" aria-label="${show}" aria-pressed="false"><i aria-hidden="true" class="bi bi-eye"></i></button></div>`;
    },

    toggleSecret(id, btn) {
        const inp = document.getElementById(id);
        if (!inp) return;
        const reveal = inp.type === 'password';
        inp.type = reveal ? 'text' : 'password';
        btn?.setAttribute('aria-pressed', String(reveal));
        const what = btn?.dataset.secretName;
        const label = what
            ? (reveal ? this.t('common.hideWhat', 'Hide {what}', { what }) : this.t('common.showWhat', 'Show {what}', { what }))
            : (reveal ? this.t('common.hide', 'Hide') : this.t('common.show', 'Show'));
        btn?.setAttribute('title', label);
        btn?.setAttribute('aria-label', label);
        const ic = btn?.querySelector('i');
        if (ic) ic.className = `bi ${reveal ? 'bi-eye-slash' : 'bi-eye'}`;
    },

    _act(method, args = [], opts = {}) {
        let s = `data-action="${method}"`;
        if (args.length) s += ` data-args="${this._esc(JSON.stringify(args))}"`;
        if (opts.el) s += ' data-with-el';
        if (opts.value) s += ' data-with-value';
        if (opts.on) s += ` data-on="${this._esc(opts.on)}"`;
        if (opts.guard) s += ` data-guard="${this._esc(opts.guard)}"`;
        return s;
    },

    // Repoint a live element's delegated action (a shared modal's Save button
    // serves several editors). Never set a handler attribute from script: the CSP
    // refuses a handler attribute set from script exactly like a static one
    // — the endpoint modal's Save was dead that way in 3.82.0–3.83.0.
    _setAction(el, method, args = []) {
        if (!el) return;
        el.removeAttribute('onclick');
        el.dataset.action = method;
        if (args.length) el.dataset.args = JSON.stringify(args); else delete el.dataset.args;
    },

    // Small DOM helpers reachable from markup (data-action) so no template
    // needs an inline handler for a one-liner.
    _selectEl(el) { el.select(); },
    _removeRow(el) { const tr = el.closest('tr'); if (tr) tr.remove(); },
    _clearHtml(id) { const el = document.getElementById(id); if (el) el.innerHTML = ''; },

    setupEventListeners() {
        this._wireActionDelegation();
        // Register search (debounced)
        document.getElementById('registerSearch').addEventListener('input', (e) => {
            this.registerSearchPage = 1;
            this._debouncedRenderRegisters();
        });

        document.getElementById('categoryFilter').addEventListener('change', () => {
            this.registerSearchPage = 1;
            this.renderRegistersTable(); // Immediate for dropdown
        });

        // Query Modal button (in Registers page header)
        document.getElementById('queryRegisterBtn').addEventListener('click', () => this.openQueryModal());

        // Query button (in modal)
        document.getElementById('queryBtn').addEventListener('click', () => this.queryRegister());

        // Enter key for query
        document.getElementById('queryAddress').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.queryRegister();
        });

        // Save registers button
        document.getElementById('saveRegistersBtn').addEventListener('click', () => this.saveSelectedRegisters());

        // Edit Modal
        document.getElementById('modalSave').addEventListener('click', () => this.saveRegisterEdit());
        document.getElementById('editWidget').addEventListener('change', (e) => this.toggleGaugeOptions('edit', e.target.value));

        // Add Modal
        document.getElementById('addModalSave').addEventListener('click', () => this.saveNewRegister());
        document.getElementById('addWidget').addEventListener('change', (e) => this.toggleGaugeOptions('add', e.target.value));

        // Config Page - Search (debounced)
        document.getElementById('configSearch').addEventListener('input', (e) => {
            this.configSearch = e.target.value.toLowerCase();
            this._debouncedRenderSelectedList();
        });

        // Raw Config Modal
        document.getElementById('rawConfigBtn').addEventListener('click', () => this.openRawConfigModal());
        document.getElementById('rawConfigFormat').addEventListener('click', () => this.formatRawConfig());
        document.getElementById('rawConfigSave').addEventListener('click', () => this.saveRawConfig());

        // Live validation for raw config editor
        document.getElementById('rawConfigEditor').addEventListener('input', () => this.validateRawConfig());

        // Customize Dashboard Modal
        document.getElementById('customizeDashBtn').addEventListener('click', () => this.openCustomizeDashModal());
        document.getElementById('customizeDashSave').addEventListener('click', () => this.saveCustomizeDash());
        // Compact density: class on the grid, persisted; aria-pressed tracks state.
        const densBtn = document.getElementById('dashboardDensityToggle');
        const applyDensity = (on) => {
            document.getElementById('dashboardGrid')?.classList.toggle('compact', on);
            densBtn?.setAttribute('aria-pressed', String(on));
            densBtn?.classList.toggle('active', on);
        };
        applyDensity(localStorage.getItem('mbg-dashboard-density') === 'compact');
        densBtn?.addEventListener('click', () => {
            const on = !(localStorage.getItem('mbg-dashboard-density') === 'compact');
            localStorage.setItem('mbg-dashboard-density', on ? 'compact' : 'normal');
            applyDensity(on);
        });

        // Status indicator clicks
        document.getElementById('statusDevices')?.addEventListener('click', () => this.navigateTo('devices'));
        document.getElementById('statusMqtt').addEventListener('click', () => this.showStatusDetail('mqtt'));
        document.getElementById('statusInflux').addEventListener('click', () => this.showStatusDetail('influxdb'));
        const vmPill = document.getElementById('statusVmeter');
        if (vmPill) vmPill.addEventListener('click', () => this.navigateTo('vmeters'));

        // Global keyboard shortcuts
        document.addEventListener('keydown', (e) => this.handleKeyboardShortcuts(e));

        // Config page main tabs (Settings/Registers)
        this.setupConfigMainTabs();
        this._tabAria('#configSubtabs');     // initial state, before any switch
        this._tabAria('#deviceRegTabs');

        // Settings form listeners
        this.setupSettingsListeners();
    },

    handleKeyboardShortcuts(e) {
        // Escape - close active modal
        if (e.key === 'Escape') {
            const activeModal = document.querySelector('.modal.active');
            if (activeModal) {
                e.preventDefault();
                this.closeModal();
            }
        }

        // Enter - confirm in modals, but ONLY from a text field: with focus on
        // Cancel or the ✕ close, preventDefault suppressed the button's native
        // activation and SAVED instead — a keyboard user pressing Enter on
        // "Cancel" committed the edit (audit 2026-10-01)
        if (e.key === 'Enter' && e.target.tagName === 'INPUT'
                && !['checkbox', 'radio', 'button', 'submit'].includes(e.target.type)) {
            const registerModal = document.getElementById('registerModal');
            const addModal = document.getElementById('addRegisterModal');
            const customizeModal = document.getElementById('customizeDashModal');

            if (registerModal && registerModal.classList.contains('active')) {
                e.preventDefault();
                this.saveRegisterEdit();
            } else if (addModal && addModal.classList.contains('active')) {
                e.preventDefault();
                this.saveNewRegister();
            } else if (customizeModal && customizeModal.classList.contains('active')) {
                e.preventDefault();
                this.saveCustomizeDash();
            }
        }
    },

    connectWebSocket() {
        if (this._sessionDead) return;       // the login screen owns the page now
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.ws = new WebSocket(wsUrl);
        let opened = false;                  // a handshake the server refused never opens

        this.ws.onopen = () => {
            opened = true;
            this.updateConnectionStatus(true);

            // Show reconnected banner if was disconnected
            if (this.wasDisconnected) {
                this.showConnectionBanner('connected',
                    this.t('conn.restored', '✓ Connection restored'));
                this.wasDisconnected = false;
            }
        };

        this.ws.onclose = () => {
            this.updateConnectionStatus(false);

            // ONE toast per outage, not one per 3s retry (the retry loop below
            // fires onclose on every failed attempt — same edge-triggered
            // discipline loadStatus uses via _statusLost). The statusbar dot
            // keeps showing the live state throughout.
            if (!this.wasDisconnected) {
                this.wasDisconnected = true;
                this.showConnectionBanner('disconnected',
                    this.t('conn.lost', '⚠ Connection lost. Reconnecting…'));
            }

            // Reconnect after 3 seconds — unless the handshake was refused
            // because the session is dead (the server answers 403 before
            // accepting; the browser only sees "closed"), in which case
            // _verifySession stops the page and shows the login screen.
            const retry = () => setTimeout(() => this.connectWebSocket(), 3000);
            if (opened) retry();
            else this._verifySession().then((alive) => { if (alive) retry(); });
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        this.ws.onmessage = (event) => {
            let msg;
            try { msg = JSON.parse(event.data); }
            catch (e) { return; }   // ignore malformed frames
            this.handleWebSocketMessage(msg);
        };
    },

    // ── Dashboard device dimension (Phase B) ───────────────────────────────
    // Address spaces overlap across devices (MQTT sources use synthetic 1,2,3…),
    // so the dashboard keeps its OWN store for a non-primary device instead of
    // merging into currentValues (which stays the primary's store — Monitor and
    // the registers table contractually read it).
    _dashDeviceId() {
        return this.dashDevice || this._primaryDeviceId();
    },
    _dashIsPrimary() {
        return this._dashDeviceId() === this._primaryDeviceId();
    },
    _dashStore() {
        return this._dashIsPrimary() ? this.currentValues : (this.dashValues || {});
    },
    // The dashboard's OWN register list — decoupled from selectedRegisters,
    // which is contextual to the Measurements page (it gets REPLACED when you
    // browse another device's measurements; the dashboard must not follow it).
    _dashRegs() {
        return this.dashRegisters || [];
    },
    async _refreshDashRegisters() {
        const dev = this._dashDeviceId();
        try {
            const qs = this._dashIsPrimary() ? '' : ('?device=' + encodeURIComponent(dev));
            const r = await fetch('/api/registers/selected' + qs);
            if (r.status === 404 && this.dashDevice === dev) {
                // a remembered device that no longer exists — the page can
                // load before the device list that would have caught it
                this.dashDevice = null;
                try { localStorage.removeItem('mbg-dash-device'); } catch (e2) { /* private mode */ }
                return this._refreshDashRegisters();
            }
            const d = await r.json();
            // last device wins: a slow answer for a device the operator has
            // already switched away from must not put ITS labels on the new
            // device's values
            if (dev !== this._dashDeviceId()) return;
            this.dashRegisters = d.registers || [];
            // how the template presents this unit (sections, bitmasks, alarms)
            this.dashDisplay = d.display || {};
        } catch (e) { this.dashRegisters = this.dashRegisters || []; }
    },
    async _setDashDevice(id) {
        const dev = id || this._primaryDeviceId();
        this.dashDevice = dev === this._primaryDeviceId() ? null : dev;
        try { localStorage.setItem('mbg-dash-device', this.dashDevice || ''); } catch (e) { /* private mode */ }
        this.valueHistory = {};                    // sparkline history is per device
        this.dashValues = {};
        // last-wins: rapid chip switching fires overlapping fetches — a slow
        // /api/values for the PREVIOUS device must not land in the new one's
        // store (audit 2026-10-01)
        const seq = this._dashSeq = (this._dashSeq || 0) + 1;
        const jobs = [this._refreshDashRegisters()];
        if (this.dashDevice) {
            jobs.push(fetch('/api/values?device=' + encodeURIComponent(dev))
                .then(r => r.json())
                // the snapshot only fills what the socket has not delivered
                // yet — a value pushed while the fetch was pending is newer
                .then(d => { if (seq === this._dashSeq) this.dashValues = { ...(d.values || {}), ...(this.dashValues || {}) }; })
                .catch(() => {}));
        }
        await Promise.all(jobs);
    },

    handleWebSocketMessage(msg) {
        if (msg.type === 'init' || msg.type === 'data') {
            // Route by device: primary messages feed currentValues (as always);
            // the ACTIVE dashboard device also feeds the dashboard store +
            // sparkline history. Everything else is ignored client-side.
            if (msg.type === 'init' && msg.device) this._primaryIdHint = msg.device;
            const msgDev = msg.device || this._primaryDeviceId();
            const isPrimary = msgDev === this._primaryDeviceId();
            const isDashActive = msgDev === this._dashDeviceId();
            if (msg.values && (isPrimary || isDashActive)) {
                const timestamp = Date.now();
                for (const [addr, data] of Object.entries(msg.values)) {
                    if (isPrimary) this.currentValues[addr] = data;
                    if (isDashActive) {
                        if (!isPrimary) (this.dashValues = this.dashValues || {})[addr] = data;
                        // Store history for chart widgets (active dash device only)
                        if (!this.valueHistory[addr]) {
                            this.valueHistory[addr] = [];
                        }
                        if (typeof data.value === 'number') {
                            this.valueHistory[addr].push({ time: timestamp, value: data.value });
                            if (this.valueHistory[addr].length > this.maxHistoryPoints) {
                                this.valueHistory[addr].shift();
                            }
                        }
                    }
                }
            }

            // Update last update time
            if (msg.ts || msg.timestamp) {
                const lastUpdateEl = document.getElementById('lastUpdate');
                if (lastUpdateEl) {
                    // `ts` is epoch seconds; `timestamp` is the server's local
                    // time without a zone (read as the browser's — wrong
                    // across zones), kept only for an older server
                    const when = msg.ts ? new Date(msg.ts * 1000) : new Date(msg.timestamp);
                    lastUpdateEl.textContent = this.t('status.lastUpdate', 'Last update:') + ' ' + when.toLocaleTimeString(this._loc());
                }
            }

            // Update dashboard if active — fleet face harvests hero values
            // from the same broadcast, device face repaints its widgets
            if (this.currentPage === 'dashboard') {
                if (this._fleetVisible && this._fleetVisible()) this._fleetIngest(msg);
                else this.updateDashboard();
            }

            // Update the measurements table if it is visible. Like the Monitor it
            // lives only inside the device workspace now (currentPage stays
            // 'devices'), so gate on the register view being laid out.
            if (this._registersVisible && this._registersVisible()) {
                this.updateRegistersValues();
            }

            // Update monitor if visible. It lives only inside the device
            // workspace now (currentPage stays 'devices'), so gate on the monitor
            // page being laid out, not on a 'monitor' top-nav page.
            if (this._monitorVisible && this._monitorVisible()) {
                this.onMonitorDataUpdate();
            }
        } else if (msg.type === 'ping') {
            this.ws.send(JSON.stringify({ type: 'pong' }));
        }
    },

    updateConnectionStatus(connected) {
        const status = document.getElementById('connectionStatus');
        if (!status) return;

        if (connected) {
            status.classList.add('connected');
            status.classList.remove('disconnected');
            status.innerHTML = '<i class="bi bi-circle-fill" aria-hidden="true"></i> '
                + this._esc(this.t('status.connected', 'connected'));
        } else {
            status.classList.remove('connected');
            status.classList.add('disconnected');
            status.innerHTML = '<i class="bi bi-circle-fill" aria-hidden="true"></i> '
                + this._esc(this.t('status.disconnected', 'disconnected'));
        }
        // The dashboard keeps showing its last numbers after a disconnect with
        // nothing marking them stale — the page people actually watch. Dim it
        // and stamp WHEN the data is from (audit 2026-10-01).
        document.body.classList.toggle('ws-stale', !connected);
        let badge = document.getElementById('staleBadge');
        if (!connected) {
            if (!badge) {
                badge = document.createElement('div');
                badge.id = 'staleBadge';
                badge.className = 'stale-badge';
                badge.setAttribute('role', 'status');
                const grid = document.getElementById('dashboardGrid');
                (grid?.parentElement || document.body).insertBefore(badge, grid || null);
            }
            badge.textContent = this.t('status.asOf', 'Live updates lost — values as of')
                + ' ' + new Date().toLocaleTimeString(this._loc());
        } else if (badge) {
            badge.remove();
        }
    },

    // ═══════════════════ Devices (Tier 2) ═══════════════════

    _primaryDeviceId() {
        // the device list, else the id the server named in its WS init; ''
        // means "the primary" to every API route, so it is a safe default
        return (this._devices || []).find(d => d.primary)?.id || this._primaryIdHint || '';
    }
});
