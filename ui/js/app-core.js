/**
 * Janitza UMG 512-PRO Monitor - Frontend Application
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
        this.queryHistory = [];
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

        // Theme state
        this.theme = localStorage.getItem('janitza-theme') || 'auto';
        this.wasDisconnected = false;

        // Dashboard view state (cards or table)
        this.dashboardView = localStorage.getItem('janitza-dashboard-view') || 'cards';
        // Dashboard device (Phase B): null = primary; persisted per browser
        this.dashDevice = localStorage.getItem('janitza-dash-device') || null;
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
            },
            current: {
                dangerLow: null, warningLow: null, warningHigh: 90, dangerHigh: 100,
                type: 'percent', unit: 'A'
            },
            power: {
                dangerLow: null, warningLow: null, warningHigh: 90, dangerHigh: 100,
                type: 'percent', unit: 'kW'
            }
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
            btn.dataset.originalText = btn.textContent;
            btn.innerHTML = '<span class="btn-spinner"></span> Saving...';
        } else {
            btn.disabled = false;
            btn.textContent = originalText || btn.dataset.originalText || 'Save';
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
        localStorage.setItem('janitza-theme', theme);

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

        // Simple toggle: cycles dark → light → auto → dark
        toggle?.addEventListener('click', () => {
            const cycle = { dark: 'light', light: 'auto', auto: 'dark' };
            this.applyTheme(cycle[this.theme]);
        });
    },

    // ============ Connection Banner ============

    showConnectionBanner(type, text) {
        // The dedicated banner element was retired — surface connection state
        // via toasts (the statusbar dot tracks it too).
        this.showToast(type === 'connected' ? 'success' : 'error', text);
    },

    hideConnectionBanner() {
        const banner = document.getElementById('connectionBanner');
        if (banner) {
            banner.classList.remove('visible');
        }
    },

    // ============ Modal Helpers ============

    _modalFocusables(el) {
        return [...el.querySelectorAll(
            'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
            'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
            .filter(n => n.offsetParent !== null);
    },

    openModal(modalId) {
        const modal = document.getElementById(modalId);
        if (!modal) return;
        this._modalReturnFocus = document.activeElement;      // restore on close
        modal.classList.add('active');
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
        setTimeout(() => { const f = this._modalFocusables(modal); (f[0] || dialog).focus(); }, 30);
        // Trap Tab within the dialog; Escape closes.
        const onKey = (e) => {
            if (e.key === 'Escape') { e.preventDefault(); this.closeModal(modalId); return; }
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
        const modal = modalId ? document.getElementById(modalId) : document.querySelector('.modal.active');
        if (!modal) return;
        modal.classList.remove('active');
        document.body.classList.remove('modal-open');
        const dialog = modal.querySelector('.modal-content') || modal;
        dialog.removeAttribute('aria-modal');
        if (modal._trapHandler) {
            modal.removeEventListener('keydown', modal._trapHandler);
            modal._trapHandler = null;
        }
        // Return focus to whatever opened the modal (don't strand the caret).
        const ret = this._modalReturnFocus;
        this._modalReturnFocus = null;
        if (ret && typeof ret.focus === 'function') setTimeout(() => ret.focus(), 0);
    },

    // ── First-run "connect your meter" prompt ──────────────────────────────
    // Shows once per session only when Modbus has never connected (fresh/
    // misconfigured deploy). Auto-closes the moment a read succeeds, and never
    // appears on a configured system having a temporary outage.
    _maybeFirstRun(modbus) {
        if (modbus && modbus.last_success_ts) {
            this._everConnected = true;
            this._closeFirstRun();
            return;
        }
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
        this.openDeviceDetail(this._primaryDeviceId()).then(() => this._switchDeviceTab('edit'));
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
        const saved = localStorage.getItem('janitza-lang') || this._defaultLang;
        await this.setLanguage(saved, false);
        this._renderLangSelector();
    },

    async _fetchLang(code) {
        try { const r = await fetch(`/api/languages/${code}`); return r.ok ? await r.json() : {}; }
        catch (e) { return {}; }
    },

    t(key, fallback) {
        return (this._t && this._t[key]) || fallback || key;
    },

    async setLanguage(code, persist = true) {
        const sel = (code && code !== 'en') ? await this._fetchLang(code) : this._tEn;
        this._t = { ...this._tEn, ...sel };          // selected overrides the English base
        this._lang = code;
        if (persist) localStorage.setItem('janitza-lang', code);
        document.documentElement.lang = code;
        this.applyTranslations();
        this._renderLangSelector();
        // Re-render the current page only on a user-initiated switch (persist), so
        // dynamic t() strings update without double-rendering during initial load.
        if (persist && this.currentPage) this.navigateTo(this.currentPage);
    },

    applyTranslations() {
        document.querySelectorAll('[data-i18n]').forEach(el => {
            if (el.dataset.i18nOrig === undefined) el.dataset.i18nOrig = el.textContent;
            el.textContent = this.t(el.getAttribute('data-i18n'), el.dataset.i18nOrig);
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            if (el.dataset.i18nOrigPh === undefined) el.dataset.i18nOrigPh = el.getAttribute('placeholder') || '';
            el.setAttribute('placeholder', this.t(el.getAttribute('data-i18n-placeholder'), el.dataset.i18nOrigPh));
        });
        document.querySelectorAll('[data-i18n-title]').forEach(el => {
            if (el.dataset.i18nOrigTitle === undefined) el.dataset.i18nOrigTitle = el.getAttribute('title') || '';
            el.setAttribute('title', this.t(el.getAttribute('data-i18n-title'), el.dataset.i18nOrigTitle));
        });
    },

    _renderLangSelector() {
        const el = document.getElementById('langSelect');
        if (!el) return;
        if (!this._langs.length) { el.style.display = 'none'; return; }
        el.innerHTML = this._langs.map(l =>
            `<option value="${l.code}" ${l.code === this._lang ? 'selected' : ''}>${l.flag || ''} ${this._esc(l.nativeName || l.name)}</option>`).join('');
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
        // dashboard device dimension: restore the persisted selection (its
        // registers + value snapshot), then render
        await this._setDashDevice(this.dashDevice || null);
        this.updateDashboard();
        this.renderDashDeviceChips();

        // Start status polling
        setInterval(() => this.loadStatus(), 5000);
    },

    async loadConfig() {
        try {
            const response = await fetch('/api/config');
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
                this.navigateTo(page);
            });
        });
    },

    navigateTo(page) {
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

        // Update pages
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.getElementById(`${page}Page`)?.classList.add('active');

        this.currentPage = page;

        // stop any virtual-meter observability polling when leaving the page
        if (page !== 'vmeters' && this._stopVmPolls) this._stopVmPolls();
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
            if (!detailOpen && !regOpen) {
                this._showDevicesList();
                this.renderDevicesList();
            }
        } else if (page === 'config') {
            this.loadSettingsConfig();
            this.switchConfigTab('mqtt');       // Devices moved out; land on MQTT
        } else if (page === 'monitor') {
            this.initMonitorPage();
        } else if (page === 'history') {
            this.initHistoryPage();
        } else if (page === 'energy') {
            this.initEnergyPage();
        } else if (page === 'vmeters') {
            this.renderVirtualMeters();
        } else if (page === 'templates') {
            this.renderTemplateManager();
        } else if (page === 'status') {
            this.initStatusPage();
        } else if (page === 'diagnostics') {
            this.initDiagnosticsPage();
        }
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
    _fmtNum(value, decimals = 2) {
        if (typeof value !== 'number' || isNaN(value)) return '--';
        return value.toLocaleString(undefined, {
            minimumFractionDigits: decimals, maximumFractionDigits: decimals });
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

    // Delegated actions: elements rendered with data-action="method" dispatch to
    // app.method(...) — replaces inline onclick="app.m('${esc(x)}')" handlers.
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
    _wireActionDelegation() {
        const dispatch = (ev) => {
            const el = ev.target.closest('[data-action]');
            if (!el) return;
            const guard = el.dataset.guard;
            if (guard && ev.target.closest(guard) !== el && ev.target.closest(guard)) return;
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
            fn.apply(this, args);
        };
        document.addEventListener('click', dispatch);
        // Enter on non-<button> actionable elements (div[role=button] rows) —
        // real buttons already synthesize a click on Enter natively.
        document.addEventListener('keydown', (ev) => {
            if (ev.key !== 'Enter') return;
            const el = ev.target.closest('[data-action]');
            if (el && 'keyEnter' in el.dataset) dispatch(ev);
        });
    },

    // Build the data-action attribute string for a template literal:
    //   `<button ${this._act('deleteDevice', [d.id])}>` — args JSON-encoded and
    // attribute-escaped in one place so call sites can't get the escaping wrong.
    _act(method, args = [], opts = {}) {
        let s = `data-action="${method}"`;
        if (args.length) s += ` data-args="${this._esc(JSON.stringify(args))}"`;
        if (opts.el) s += ' data-with-el';
        if (opts.guard) s += ` data-guard="${this._esc(opts.guard)}"`;
        return s;
    },

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
        document.getElementById('dashboardViewToggle')?.addEventListener('click', () => this.toggleDashboardView());
        // Compact density: class on the grid, persisted; aria-pressed tracks state.
        const densBtn = document.getElementById('dashboardDensityToggle');
        const applyDensity = (on) => {
            document.getElementById('dashboardGrid')?.classList.toggle('compact', on);
            densBtn?.setAttribute('aria-pressed', String(on));
            densBtn?.classList.toggle('active', on);
        };
        applyDensity(localStorage.getItem('janitza-dashboard-density') === 'compact');
        densBtn?.addEventListener('click', () => {
            const on = !(localStorage.getItem('janitza-dashboard-density') === 'compact');
            localStorage.setItem('janitza-dashboard-density', on ? 'compact' : 'normal');
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

        // Enter - confirm in modals (but not in textareas)
        if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') {
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
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            this.updateConnectionStatus(true);

            // Show reconnected banner if was disconnected
            if (this.wasDisconnected) {
                this.showConnectionBanner('connected', '✓ Connection restored');
                setTimeout(() => this.hideConnectionBanner(), 2500);
                this.wasDisconnected = false;
            }
        };

        this.ws.onclose = () => {
            this.updateConnectionStatus(false);
            this.wasDisconnected = true;

            // Show disconnect banner
            this.showConnectionBanner('disconnected', '⚠ Connection lost. Reconnecting...');

            // Reconnect after 3 seconds
            setTimeout(() => this.connectWebSocket(), 3000);
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
            const d = await (await fetch('/api/registers/selected' + qs)).json();
            this.dashRegisters = d.registers || [];
        } catch (e) { this.dashRegisters = this.dashRegisters || []; }
    },
    async _setDashDevice(id) {
        const dev = id || this._primaryDeviceId();
        this.dashDevice = dev === this._primaryDeviceId() ? null : dev;
        localStorage.setItem('janitza-dash-device', this.dashDevice || '');
        this.valueHistory = {};                    // sparkline history is per device
        this.dashValues = {};
        const jobs = [this._refreshDashRegisters()];
        if (this.dashDevice) {
            jobs.push(fetch('/api/values?device=' + encodeURIComponent(dev))
                .then(r => r.json()).then(d => { this.dashValues = d.values || {}; })
                .catch(() => {}));
        }
        await Promise.all(jobs);
    },

    handleWebSocketMessage(msg) {
        if (msg.type === 'init' || msg.type === 'data') {
            // Route by device: primary messages feed currentValues (as always);
            // the ACTIVE dashboard device also feeds the dashboard store +
            // sparkline history. Everything else is ignored client-side.
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
            if (msg.timestamp) {
                const lastUpdateEl = document.getElementById('lastUpdate');
                if (lastUpdateEl) {
                    lastUpdateEl.textContent = 'Last update: ' + new Date(msg.timestamp).toLocaleTimeString();
                }
            }

            // Update dashboard if active
            if (this.currentPage === 'dashboard') {
                this.updateDashboard();
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

        const icon = status.querySelector('i');
        if (connected) {
            status.classList.add('connected');
            status.classList.remove('disconnected');
            status.innerHTML = '<i class="bi bi-circle-fill"></i> Connected';
        } else {
            status.classList.remove('connected');
            status.classList.add('disconnected');
            status.innerHTML = '<i class="bi bi-circle-fill"></i> Disconnected';
        }
    },

    // ═══════════════════ Devices (Tier 2) ═══════════════════

    _primaryDeviceId() {
        return (this._devices || []).find(d => d.primary)?.id || 'umg512';
    }
});
