/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The security posture, judged once.
// Pure: no DOM, no fetch — Settings → Security reads it, Node tests it.
(function (root) {
    'use strict';
    const UNUSED_TOKEN_DAYS = 30;
    const LOCKOUT_HIGH = 20;

    /**
     * sec: /api/config/security · ui: /api/config/ui-security
     * ctx: {https: page served over HTTPS, now: s, passkeys: [], tokens: []}
     */
    function posture(sec, ui, ctx) {
        sec = sec || {}; ui = ui || {}; ctx = ctx || {};
        const now = ctx.now || Date.now() / 1000;
        const issues = [];
        const add = (sev, tab, key, what) => issues.push({ sev, tab, key, what });
        const login = !!ui.auth_enabled;
        const canonicalHttps = /^https:\/\//i.test(String(ui.canonical_url || ''));
        // served: the gateway serves it AND this page came over it (enabled but
        // a plain-HTTP page means a restart is pending or the certificate failed)
        const pending = !!ui.tls_enabled && !ctx.https;
        const https = !!ctx.https || (!pending && canonicalHttps);
        const allow = (sec.allowlist || []).filter(Boolean);
        const tokens = ctx.tokens || [], passkeys = ctx.passkeys || [];
        const tokensKnown = ctx.tokens !== null;

        if (!login) add('warn', 'accounts', 'login-off', { k: 'security.why.loginOff', d: 'Login is off: anyone who reaches the gateway can change its configuration' });
        else if (pending) add('warn', 'network', 'https-pending', { k: 'security.why.httpsPending', d: 'HTTPS is enabled but not active: restart the gateway, or check its certificate' });
        else if (!https) add('warn', 'network', 'no-https', { k: 'security.why.noHttps', d: 'Login without HTTPS: passwords travel unencrypted' });
        if (sec.allow_nonlan_http_devices) add('warn', 'writes', 'nonlan-http', { k: 'security.why.nonlanHttp', d: 'HTTP devices outside the LAN are allowed (an SSRF path)' });
        const stale = tokens.filter(t => !t.last_used && t.created && now - t.created > UNUSED_TOKEN_DAYS * 86400);
        if (stale.length) add('warn', 'access', 'unused-tokens', { k: 'security.why.unusedTokens', d: '{n} service token(s) never used in {d} days — revoke what nobody needs', p: { n: stale.length, d: UNUSED_TOKEN_DAYS } });

        const tabSev = { network: 'ok', accounts: 'ok', access: 'ok', writes: 'ok', audit: 'ok' };
        issues.forEach(i => { tabSev[i.tab] = 'warn'; });
        const pills = {
            login: { on: login, sev: login ? 'ok' : 'warn', tab: 'accounts' },
            https: { on: https, how: pending ? 'pending' : ui.tls_enabled ? 'served' : ctx.https ? 'proxy' : canonicalHttps ? 'canonical' : 'off',
                     sev: pending || (!https && login) ? 'warn' : https ? 'ok' : 'off', tab: 'network' },
            network: { open: !allow.length, n: allow.length, sev: 'off', tab: 'network' },
            writes: { armed: !!sec.allow_writes, sev: sec.allow_writes ? 'info' : 'off', tab: 'writes' },
            access: { passkeys: passkeys.length, tokens: tokensKnown ? tokens.length : null, sev: stale.length ? 'warn' : 'off', tab: 'access' },
        };
        const lockoutHigh = (ui.lockout_threshold || 0) > LOCKOUT_HIGH;
        return { issues, tabSev, pills, login, https, lockoutHigh, warnings: issues.length };
    }

    // an audit status in one of three words; any sign-in that did not
    // succeed (password or passkey) is "denied"
    const DENIED = ['denied', 'invalid', 'locked', '401', '403', 'unknown credential', 'verification failed', 'account gone', 'refused'];
    function auditResult(status, action) {
        const s = String(status || '').toLowerCase();
        if (s === 'ok') return 'ok';
        if (DENIED.some(w => s.includes(w)) || /^(login|passkey)/i.test(String(action || ''))) return 'denied';
        return 'failed';
    }

    const api = { posture, auditResult, UNUSED_TOKEN_DAYS };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.SecurityModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
