// auth domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    installApiAuth() {
        // If the server requires an API key for writes, attach it to every
        // non-GET request and prompt once (localStorage) on a 401. No-op when
        // the server is open. Robust to Headers/Request inputs; never mutates
        // the caller's options object.
        const orig = window.fetch.bind(window);
        const KEY = 'janitza-api-key';
        window.fetch = (input, opts = {}) => {
            const method = ((opts && opts.method) || (input && input.method) || 'GET').toUpperCase();
            const writing = method !== 'GET' && method !== 'HEAD';
            if (!writing || (typeof Request !== 'undefined' && input instanceof Request)) {
                return orig(input, opts);
            }
            const send = (key) => {
                const h = new Headers((opts && opts.headers) || {});
                if (key) h.set('X-API-Key', key);
                return orig(input, { ...opts, headers: h });
            };
            return send(localStorage.getItem(KEY)).then((res) => {
                if (res.status !== 401) return res;
                const k = window.prompt('This action needs the API key:');
                if (!k) return res;
                localStorage.setItem(KEY, k);
                return send(k);
            });
        };
    },

    // ── login / auth ────────────────────────────────────────────────────────
    async _checkAuth() {
        // returns true if we may proceed (auth off, or authenticated)
        try {
            const s = await (await fetch('/api/auth/status')).json();
            this._authEnabled = !!s.enabled;
            this._role = s.role;
            return !s.enabled || !!s.role;
        } catch (e) {
            return true;   // status endpoint is always open; if it fails, don't lock the UI
        }
    },

    _showLogin(message) {
        let ov = document.getElementById('loginOverlay');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'loginOverlay';
            ov.className = 'login-overlay';
            ov.innerHTML = `
              <form class="login-card" id="loginForm" autocomplete="on">
                <div class="login-logo"><i class="bi bi-lightning-charge-fill"></i> ${this.t('app.title', 'Modbus Gateway')}</div>
                <h3>${this.t('login.title', 'Sign in')}</h3>
                <input class="input" id="loginUser" placeholder="${this.t('login.user', 'Username')}" autocomplete="username" autofocus>
                <input class="input" id="loginPass" type="password" placeholder="${this.t('login.pass', 'Password')}" autocomplete="current-password">
                <div class="login-error" id="loginError"></div>
                <button class="btn btn-primary" type="submit" id="loginBtn">${this.t('login.submit', 'Sign in')}</button>
                <button class="btn btn-secondary" type="button" id="passkeyBtn" style="display:none;margin-top:8px;">
                    <i class="bi bi-fingerprint"></i> ${this.t('login.passkey', 'Sign in with passkey')}</button>
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
                err.textContent = this.t('login.lockedOut', 'Too many attempts. Try again in') +
                    ` ${Math.ceil(s / 60)} min.`;
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
        const label = prompt(this.t('passkeys.labelPrompt', 'A name for this passkey (e.g. "laptop", "phone"):'), '');
        if (label === null) return;
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
            if (!rows.length) {
                body.innerHTML = `<tr><td colspan="5" class="diag-empty">${this._esc(
                    this.t('passkeys.empty', 'No passkeys yet. Register one from a secure context (localhost or an HTTPS hostname).'))}</td></tr>`;
                return;
            }
            body.innerHTML = rows.map(p => `<tr>
                <td>${this._esc(p.label)}</td>
                <td>${this._esc(p.user)} <span class="badge badge-secondary">${this._esc(p.role)}</span></td>
                <td class="mono" style="font-size:11.5px;">${this._esc(p.rp_id)}</td>
                <td style="white-space:nowrap;">${this._esc(new Date(p.created * 1000).toLocaleDateString())}</td>
                <td style="text-align:right;"><button class="btn btn-ghost btn-sm"
                    onclick="app.deletePasskey('${this._esc(p.id)}')" title="${this._esc(this.t('common.delete', 'Delete'))}">
                    <i class="bi bi-trash"></i></button></td>
            </tr>`).join('');
        } catch (e) {
            body.innerHTML = `<tr><td colspan="5" class="err">${this._esc(String(e.message || e))}</td></tr>`;
        }
    },

    async deletePasskey(id) {
        if (!confirm(this.t('passkeys.deleteConfirm', 'Delete this passkey? Sign-in with it stops working immediately.'))) return;
        try {
            const r = await fetch(`/api/auth/passkeys/${encodeURIComponent(id)}`, { method: 'DELETE' });
            if (!r.ok) throw new Error(await r.text());
            this.loadPasskeys();
        } catch (e) {
            this.showToast('error', this.t('passkeys.title', 'Passkeys'), String(e.message || e));
        }
    },
});
