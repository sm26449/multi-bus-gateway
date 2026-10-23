/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// auth domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    installApiAuth() {
        // If the server requires an API key for writes, attach it to every
        // non-GET request and prompt once (localStorage) on a 401. No-op when
        // the server is open. Robust to Headers/Request inputs; never mutates
        // the caller's options object.
        const orig = window.fetch.bind(window);
        const KEY = 'mbg-api-key';
        window.fetch = (input, opts = {}) => {
            const method = ((opts && opts.method) || (input && input.method) || 'GET').toUpperCase();
            const writing = method !== 'GET' && method !== 'HEAD';
            if (writing) this._writeGen = (this._writeGen || 0) + 1;
            // the key belongs to THIS gateway: never attach it to another origin
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            const sameOrigin = url.startsWith('/') || url.startsWith(location.origin + '/') || url === location.origin;
            if (!writing || !sameOrigin || (typeof Request !== 'undefined' && input instanceof Request)) {
                // Reads too: a 401 on any API read with login enabled means
                // the session died (expired, revoked, or the server forgot
                // it) — go to the login screen at once instead of polling
                // into a wall until the operator happens to click.
                return orig(input, opts).then((res) => {
                    if (res.status === 401) this._verifySession();
                    return res;
                });
            }
            const send = (key) => {
                const h = new Headers((opts && opts.headers) || {});
                if (key) h.set('X-API-Key', key);
                return orig(input, { ...opts, headers: h });
            };
            return send(localStorage.getItem(KEY)).then(async (res) => {
                if (res.status === 403) this._roleRefused(res);
                if (res.status !== 401) return res;
                // A 401 with login ENABLED and no live session is a DEAD
                // SESSION (a security save revokes all sessions), not a
                // missing API key — the old prompt sent the operator hunting
                // for a key that usually does not exist. Re-login instead.
                if (!(await this._verifySession())) return res;
                const k = await this._askApiKey();
                if (!k) return res;
                localStorage.setItem(KEY, k);
                return send(k);
            });
        };
    },

    // ── login / auth ────────────────────────────────────────────────────────
    // ── dead session ────────────────────────────────────────
    // A session ends without any request from this page: it expires, a
    // security save revokes it, the server restarts without it. Until the
    // operator clicked, the page just kept polling /api/status into 401s and
    // re-dialling the WebSocket every 3 s into 403s (281 rejects in 10 min on
    // 2026-09-15). Every 401 and every rejected WebSocket handshake now asks
    // the always-open /api/auth/status; a dead session stops the page and
    // shows the login screen — the same as an explicit logout.
    async _verifySession() {
        // true = the session is alive (or login is off); false = dead, page stopped
        if (this._sessionDead) return false;
        try {
            const s = await (await fetch('/api/auth/status')).json();
            if (s.enabled && !s.role) { this._sessionLost(); return false; }
        } catch (e) { /* status unreachable: a network problem, not a dead session */ }
        return true;
    },

    _sessionLost() {
        if (this._sessionDead) return;
        this._sessionDead = true;
        if (this._statusTimer) { clearInterval(this._statusTimer); this._statusTimer = null; }
        if (this.ws) {
            this.ws.onclose = null;          // no reconnect loop on a dead session
            try { this.ws.close(); } catch (e) {}
            this.ws = null;
        }
        this.updateConnectionStatus && this.updateConnectionStatus(false);
        // "expired" only if there WAS a session — a first visit's early 401 is
        // simply "not signed in yet"
        this._showLogin(this._role ? this.t('auth.sessionExpired', 'Session expired — sign in again') : '');
    },

    // what the server lets this account do: a viewer or an operator sees a
    // configuration control's state, but cannot change it
    _isAdminView() {
        const b = document.body.classList;
        return !b.contains('role-viewer') && !b.contains('role-operator');
    },

    // true when _roleRefused explains this 403 itself — a refusal for the
    // account's role; any other 403 ("writes are disabled", "arming requires
    // authentication") is the caller's to say
    _roleSpeaks(detail) {
        return !!this._authEnabled && this._role !== 'admin'
            && typeof detail === 'string' && /read-only|operator|configuration/i.test(detail);
    },

    // A write the role may not do (a control nobody marked): say so plainly,
    // once a moment, instead of whatever the caller makes of an empty error.
    _roleRefused(res) {
        if (!this._authEnabled || this._role === 'admin') return;
        const now = Date.now();
        if (now - (this._roleRefusedAt || 0) < 4000) return;
        this._roleRefusedAt = now;
        res.clone().json().then(d => {
            if (!this._roleSpeaks(d?.detail)) return;
            this.showToast('warning', this.t('auth.notAllowed', 'Not allowed for your account'),
                this._role === 'viewer'
                    ? this.t('auth.viewerRefused', 'This account can only look. Ask an administrator.')
                    : this.t('auth.operatorRefused', 'This account runs live actions, not configuration. Ask an administrator.'));
        }).catch(() => {});
    },

    async _checkAuth() {
        // returns true if we may proceed (auth off, or authenticated)
        try {
            const s = await (await fetch('/api/auth/status')).json();
            this._authEnabled = !!s.enabled;
            this._role = s.role;
            // a read-only session sees no control it could not use: every
            // control marked data-admin / data-live is hidden by CSS (base.css)
            document.body.classList.toggle('role-viewer', !!s.enabled && s.role === 'viewer');
            document.body.classList.toggle('role-operator', !!s.enabled && s.role === 'operator');
            this._applyModules(s.modules);
            this._watchRole();
            return !s.enabled || !!s.role;
        } catch (e) {
            return true;   // status endpoint is always open; if it fails, don't lock the UI
        }
    },

    // modules (modules: in config, and the ESP nodes service set the old way):
    // listed in Settings → Modules — what each is, whether it answers, Open.
    // The tab exists only when there is one.
    _applyModules(mods) {
        const ok = (Array.isArray(mods) ? mods : []).filter(m =>
            m && /^(\/(?!\/)|https?:\/\/)/.test(m.url || '') && /^[a-z0-9-]+$/.test(m.icon || ''));
        const tab = document.getElementById('cfgTabModules');
        if (tab) tab.hidden = !ok.length;
        const host = document.getElementById('modulesList');
        if (!host) return;
        host.innerHTML = ok.map(m => `
            <div class="module-row" data-module="${this._esc(m.id)}">
                <i class="bi bi-${this._esc(m.icon)}" aria-hidden="true"></i>
                <div class="module-main">
                    <strong>${this._esc(m.name)} <span class="sink-pill off module-health" aria-live="polite">${m.health ? this._esc(this.t('modules.checking', 'checking…')) : ''}</span></strong>
                    ${m.description ? `<span class="module-desc">${this._esc(m.description)}</span>` : ''}
                    <code class="field-hint">${this._esc(m.url)}</code>
                </div>
                <a class="btn btn-primary btn-sm" href="${this._esc(m.url)}"><i class="bi bi-box-arrow-up-right" aria-hidden="true"></i> ${this._esc(this.t('common.open', 'Open'))}</a>
            </div>`).join('');
        ok.filter(m => m.health).forEach(async m => {
            const pill = host.querySelector(`.module-row[data-module="${m.id}"] .module-health`);
            let up = false;
            try { up = (await fetch(m.health, { cache: 'no-store' })).ok; } catch (e) { up = false; }
            if (!pill) return;
            pill.className = `sink-pill ${up ? 'ok' : 'warn'} module-health`;
            pill.textContent = up ? this.t('modules.up', 'answers') : this.t('modules.down', 'does not answer');
        });
    },

    // a standby gateway says so on every page: what it is, since when, why
    _watchRole() {
        if (this._roleTimer) return;
        const tick = async () => {
            let st = null;
            try { st = await (await fetch('/api/role')).json(); } catch (e) { return; }
            const el = document.getElementById('roleBanner');
            if (!el || !st) return;
            const standby = st.role === 'standby';
            el.hidden = !standby;
            document.body.classList.toggle('role-standby', standby);
            if (standby) {
                const since = st.since ? new Date(st.since * 1000).toLocaleString(this._loc()) : '';
                el.innerHTML = `<i class="bi bi-pause-circle" aria-hidden="true"></i>
                    <strong>${this._esc(this.t('role.standby', 'Standby'))}</strong> —
                    ${this._esc(this.t('role.standbyText', 'this gateway is the backup: it does not poll, write or publish; its partner does.'))}
                    <span class="role-banner-meta">${this._esc(this.t('role.since', 'Since'))} ${this._esc(since)} · ${this._esc(this.t('role.epoch', 'epoch'))} ${this._esc(String(st.epoch))}${st.set_by ? ' · ' + this._esc(st.set_by) : ''}${st.reason ? ' · ' + this._esc(st.reason) : ''}</span>`;
            }
        };
        tick();
        this._roleTimer = setInterval(tick, 10000);
    },

    _showLogin(message) {
        let ov = document.getElementById('loginOverlay');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'loginOverlay';
            ov.className = 'login-overlay';
            // Dialog semantics (a11y audit): programmatic names via real
            // labels (placeholders vanish on input and are not reliable
            // accessible names), the error announced via role=alert, and
            // aria-modal so SRs don't wander into the app behind it. No
            // Escape/close on purpose — login is not dismissible.
            ov.setAttribute('role', 'dialog');
            ov.setAttribute('aria-modal', 'true');
            ov.setAttribute('aria-labelledby', 'loginTitle');
            ov.innerHTML = `
              <form class="login-card" id="loginForm" autocomplete="on">
                <div class="login-logo"><i class="bi bi-lightning-charge-fill" aria-hidden="true"></i> ${this.t('app.title', 'Multi-Bus Gateway')}</div>
                <h3 id="loginTitle">${this.t('login.title', 'Sign in')}</h3>
                <label class="sr-only" for="loginUser">${this.t('login.user', 'Username')}</label>
                <input class="input" id="loginUser" placeholder="${this.t('login.user', 'Username')}" autocomplete="username" autofocus>
                <label class="sr-only" for="loginPass">${this.t('login.pass', 'Password')}</label>
                <input class="input" id="loginPass" type="password" placeholder="${this.t('login.pass', 'Password')}" autocomplete="current-password">
                <div class="login-error" id="loginError" role="alert"></div>
                <button class="btn btn-primary" type="submit" id="loginBtn">${this.t('login.submit', 'Sign in')}</button>
                <button class="btn btn-secondary" type="button" id="passkeyBtn" style="display:none;margin-top:8px;">
                    <i class="bi bi-fingerprint" aria-hidden="true"></i> ${this.t('login.passkey', 'Sign in with passkey')}</button>
              </form>`;
            document.body.appendChild(ov);
            document.getElementById('loginForm').addEventListener('submit', (e) => {
                e.preventDefault();
                this._doLogin();
            });
            document.getElementById('passkeyBtn').addEventListener('click', () => this.passkeyLogin());
            // arată butonul doar când există passkeys ȘI contextul e securizat
            if (this._passkeysUsable()) {
                fetch('/api/auth/status').then(r => r.json()).then(s => {
                    if (s.has_passkeys) document.getElementById('passkeyBtn').style.display = '';
                }).catch(() => {});
            }
        }
        if (message) document.getElementById('loginError').textContent = message;
        ov.style.display = 'flex';
        // move focus into the dialog (autofocus only fires on initial parse)
        setTimeout(() => document.getElementById('loginUser')?.focus(), 0);
    },

    async _doLogin() {
        const user = document.getElementById('loginUser').value;
        const pass = document.getElementById('loginPass').value;
        const err = document.getElementById('loginError');
        const btn = document.getElementById('loginBtn');
        btn.disabled = true; err.textContent = '';
        try {
            const r = await fetch('/api/auth/login', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: user, password: pass }),
            });
            if (r.status === 429) {
                const d = await r.json();
                const s = d.detail?.retry_after_s || 0;
                err.textContent = this.t('login.lockedOut', 'Too many attempts. Try again in {min} min.',
                    { min: Math.ceil(s / 60) });
                return;
            }
            if (!r.ok) {
                err.textContent = this.t('login.invalid', 'Invalid username or password.');
                return;
            }
            // success → hide overlay and run the normal init
            document.getElementById('loginOverlay').style.display = 'none';
            location.reload();
        } catch (e) {
            err.textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    },

    async logout() {
        try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
        try { localStorage.removeItem('mbg-api-key'); } catch (e) {}   // the key leaves with the session
        location.reload();
    }
});

// ── passkeys (WebAuthn) ──
Object.assign(JanitzaMonitor.prototype, {

    _b64uToBuf(s) {
        const b = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4));
        return Uint8Array.from(b, c => c.charCodeAt(0)).buffer;
    },
    _bufToB64u(buf) {
        return btoa(String.fromCharCode(...new Uint8Array(buf)))
            .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    },

    _passkeysUsable() {
        return !!window.PublicKeyCredential && window.isSecureContext;
    },

    async passkeyLogin() {
        const err = document.getElementById('loginError');
        if (err) err.textContent = '';
        try {
            const r0 = await fetch('/api/auth/passkey/login/begin', { method: 'POST' });
            const d0 = await r0.json();
            if (!r0.ok) throw new Error((d0.detail?.errors || [d0.detail?.error || r0.statusText]).join('; '));
            const pk = d0.options.publicKey || d0.options;
            pk.challenge = this._b64uToBuf(pk.challenge);
            (pk.allowCredentials || []).forEach(c => { c.id = this._b64uToBuf(c.id); });
            const cred = await navigator.credentials.get({ publicKey: pk });
            const body = {
                state: d0.state,
                credential: {
                    id: cred.id, rawId: this._bufToB64u(cred.rawId), type: cred.type,
                    clientExtensionResults: cred.getClientExtensionResults(),
                    response: {
                        clientDataJSON: this._bufToB64u(cred.response.clientDataJSON),
                        authenticatorData: this._bufToB64u(cred.response.authenticatorData),
                        signature: this._bufToB64u(cred.response.signature),
                        userHandle: cred.response.userHandle ? this._bufToB64u(cred.response.userHandle) : null,
                    },
                },
            };
            const r1 = await fetch('/api/auth/passkey/login/finish', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r1.ok) throw new Error(this.t('login.passkeyFailed', 'Passkey sign-in failed'));
            window.location.reload();
        } catch (e) {
            if (err) err.textContent = (e.name === 'NotAllowedError')
                ? this.t('login.passkeyCancelled', 'Passkey prompt cancelled.')
                : String(e.message || e);
        }
    },

    async passkeyRegister() {
        const v = await this.askValues({
            title: this.t('passkeys.addTitle', 'Add a passkey'),
            confirmLabel: this.t('common.next', 'Next'),
            fields: [{ name: 'label', kind: 'text', required: false,
                       label: this.t('passkeys.labelPrompt', 'A name for this passkey (e.g. "laptop", "phone"):') }],
        });
        if (!v) return;
        const label = v.label || '';
        try {
            if (!this._passkeysUsable()) {
                throw new Error(this.t('passkeys.insecure',
                    'Passkeys need a secure context — open the UI via localhost or an HTTPS hostname (not a plain-HTTP IP).'));
            }
            const r0 = await fetch('/api/auth/passkey/register/begin', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label }),
            });
            const d0 = await r0.json();
            if (!r0.ok) throw new Error((d0.detail?.errors || [r0.statusText]).join('; '));
            const pk = d0.options.publicKey || d0.options;
            pk.challenge = this._b64uToBuf(pk.challenge);
            pk.user.id = this._b64uToBuf(pk.user.id);
            (pk.excludeCredentials || []).forEach(c => { c.id = this._b64uToBuf(c.id); });
            const cred = await navigator.credentials.create({ publicKey: pk });
            const body = {
                state: d0.state,
                credential: {
                    id: cred.id, rawId: this._bufToB64u(cred.rawId), type: cred.type,
                    clientExtensionResults: cred.getClientExtensionResults(),
                    response: {
                        clientDataJSON: this._bufToB64u(cred.response.clientDataJSON),
                        attestationObject: this._bufToB64u(cred.response.attestationObject),
                        transports: cred.response.getTransports ? cred.response.getTransports() : [],
                    },
                },
            };
            const r1 = await fetch('/api/auth/passkey/register/finish', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const d1 = await r1.json();
            if (!r1.ok) throw new Error((d1.detail?.errors || [r1.statusText]).join('; '));
            this.showToast('success', this.t('passkeys.title', 'Passkeys'),
                           this.t('passkeys.added', 'Passkey registered.'));
            this.loadPasskeys();
        } catch (e) {
            if (e.name === 'NotAllowedError') return;   // user a anulat promptul
            this.showToast('error', this.t('passkeys.title', 'Passkeys'), String(e.message || e));
        }
    },

    async loadPasskeys() {
        const body = document.getElementById('passkeyTableBody');
        if (!body) return;
        try {
            const r = await fetch('/api/auth/passkeys');
            if (!r.ok) throw new Error(r.statusText);
            const d = await r.json();
            const rows = d.passkeys || [];
            this._secPasskeys = rows;
            if (this._secPaint) this._secPaint();
            if (!rows.length) {
                body.innerHTML = `<tr><td colspan="5" class="diag-empty">${this._esc(
                    this.t('passkeys.empty', 'No passkeys yet. Register one from a secure context (localhost or an HTTPS hostname).'))}</td></tr>`;
                return;
            }
            body.innerHTML = rows.map(p => `<tr>
                <td>${this._esc(p.label)}</td>
                <td>${this._esc(p.user)} <span class="badge badge-secondary">${this._esc(p.role)}</span></td>
                <td class="mono" style="font-size:11.5px;">${this._esc(p.rp_id)}</td>
                <td style="white-space:nowrap;">${this._esc(new Date(p.created * 1000).toLocaleDateString(this._loc()))}</td>
                <td style="text-align:right;"><button data-live class="btn btn-ghost btn-sm"
                    ${this._act('deletePasskey', [p.id])} ${this._ib(this.t('common.delete', 'Delete'), p.label)}>
                    <i aria-hidden="true" class="bi bi-trash"></i></button></td>
            </tr>`).join('');
        } catch (e) {
            body.innerHTML = `<tr><td colspan="5" class="err">${this._esc(String(e.message || e))}</td></tr>`;
        }
    },

    async deletePasskey(id) {
        if (!await this.confirmAction({
            title: this.t('passkeys.deleteTitle', 'Delete passkey'),
            message: this.t('passkeys.deleteConfirm', 'Sign-in with it stops working immediately.'),
            confirmLabel: this.t('common.delete', 'Delete'), danger: true,
        })) return;
        try {
            const r = await fetch(`/api/auth/passkeys/${encodeURIComponent(id)}`, { method: 'DELETE' });
            if (!r.ok) throw new Error(await r.text());
            this.loadPasskeys();
        } catch (e) {
            this.showToast('error', this.t('passkeys.title', 'Passkeys'), String(e.message || e));
        }
    },

    // ── service tokens (programs) ──
    async loadTokens() {
        const body = document.getElementById('tokenTableBody');
        if (!body) return;
        try {
            const r = await fetch('/api/auth/service-tokens');
            // not an admin: the tokens are unknown here, not zero
            if (r.status === 403) { this._secTokens = null; if (this._secPaint) this._secPaint(); }
            if (!r.ok) throw new Error(r.statusText);
            const rows = (await r.json()).tokens || [];
            this._secTokens = rows;
            if (this._secPaint) this._secPaint();
            if (!rows.length) {
                body.innerHTML = `<tr><td colspan="5" class="diag-empty">${this._esc(
                    this.t('tokens.empty', 'No service tokens.'))}</td></tr>`;
                return;
            }
            const when = ts => ts ? new Date(ts * 1000).toLocaleString(this._loc()) : this.t('tokens.never', 'never');
            body.innerHTML = rows.map(k => `<tr>
                <td class="mono">${this._esc(k.name)}</td>
                <td><span class="badge badge-secondary">${this._esc(k.role)}</span></td>
                <td style="white-space:nowrap;">${this._esc(when(k.created))}</td>
                <td style="white-space:nowrap;">${this._esc(when(k.last_used))}</td>
                <td style="text-align:right;"><button data-admin class="btn btn-ghost btn-sm"
                    ${this._act('tokenRevoke', [k.name])} ${this._ib(this.t('tokens.revoke', 'Revoke'), k.name)}>
                    <i aria-hidden="true" class="bi bi-trash"></i></button></td>
            </tr>`).join('');
        } catch (e) {
            body.innerHTML = `<tr><td colspan="5" class="err">${this._esc(String(e.message || e))}</td></tr>`;
        }
    },

    async tokenCreate() {
        const name = (document.getElementById('tokName')?.value || '').trim();
        const role = document.getElementById('tokRole')?.value || 'viewer';
        const out = document.getElementById('tokNew');
        try {
            const r = await fetch('/api/auth/service-tokens', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, role }),
            });
            const d = await r.json();
            if (!r.ok) throw new Error((d.detail?.errors || [d.detail || r.statusText]).join('; '));
            if (out) {
                out.style.display = '';
                out.innerHTML = `<strong>${this._esc(this.t('tokens.once', 'Copy it now: it is not shown again.'))}</strong>
                    <input type="text" class="input mono" readonly value="${this._esc(d.token)}" aria-label="${this._esc(d.name)}">`;
                out.querySelector('input')?.select();
            }
            document.getElementById('tokName').value = '';
            this.loadTokens();
        } catch (e) {
            this.showToast('error', this.t('tokens.title', 'Service tokens'), String(e.message || e));
        }
    },

    async tokenRevoke(name) {
        if (!await this.confirmAction({
            title: this.t('tokens.revokeTitle', 'Revoke token {name}', { name }),
            message: this.t('tokens.revokeConfirm', 'The program using it is refused at once.'),
            confirmLabel: this.t('tokens.revoke', 'Revoke'), danger: true,
        })) return;
        try {
            const r = await fetch(`/api/auth/service-tokens/${encodeURIComponent(name)}`, { method: 'DELETE' });
            if (!r.ok) throw new Error(await r.text());
            const out = document.getElementById('tokNew');
            if (out) { out.style.display = 'none'; out.innerHTML = ''; }
            this.loadTokens();
        } catch (e) {
            this.showToast('error', this.t('tokens.title', 'Service tokens'), String(e.message || e));
        }
    },
});
