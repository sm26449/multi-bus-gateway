// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The security posture (ui/js/security-model.js), with no browser.
//
//   node tools/e2e/security_model_test.mjs
import { createRequire } from 'module';
const M = createRequire(import.meta.url)('../../ui/js/security-model.js');
let failed = 0, n = 0;
const check = (name, cond, extra = '') => { n++; if (!cond) failed++; console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const keys = p => p.issues.map(i => i.key).join();
const NOW = 2e9;

check('login off warns', keys(M.posture({}, { auth_enabled: false }, { now: NOW })) === 'login-off');
check('login on, plain HTTP warns', keys(M.posture({}, { auth_enabled: true }, { now: NOW })) === 'no-https');
check('login on, HTTPS served and this page over it: fine', keys(M.posture({}, { auth_enabled: true, tls_enabled: true }, { https: true, now: NOW })) === '');
check('HTTPS enabled but the page came over HTTP: pending', keys(M.posture({}, { auth_enabled: true, tls_enabled: true }, { now: NOW })) === 'https-pending');
check('login on, HTTPS through a proxy: fine', keys(M.posture({}, { auth_enabled: true }, { https: true, now: NOW })) === '');
check('login on, HTTPS canonical: fine', keys(M.posture({}, { auth_enabled: true, canonical_url: 'https://gw.lan' }, { now: NOW })) === '');
check('non-LAN HTTP warns', keys(M.posture({ allow_nonlan_http_devices: true }, { auth_enabled: true, tls_enabled: true }, { https: true, now: NOW })) === 'nonlan-http');
const p = M.posture({ allow_writes: true, allowlist: [] }, { auth_enabled: true, tls_enabled: true }, { https: true, now: NOW });
check('writes armed and an open network are states, not warnings', p.warnings === 0 && p.pills.writes.armed && p.pills.network.open);
const toks = [{ name: 'old', created: NOW - 40 * 86400 }, { name: 'used', created: NOW - 40 * 86400, last_used: NOW - 10 }, { name: 'new', created: NOW - 86400 }];
const pt = M.posture({}, { auth_enabled: true, tls_enabled: true }, { https: true, now: NOW, tokens: toks });
check('tokens unknown (not an admin) read as unknown, not zero', M.posture({}, {}, { tokens: null }).pills.access.tokens === null);
check('a token never used in 30 days warns, once', keys(pt) === 'unused-tokens' && pt.issues[0].what.p.n === 1);
check('tab marks follow the warnings', pt.tabSev.access === 'warn' && pt.tabSev.network === 'ok');
check('audit results', ['ok', 'denied (role)', 'invalid credentials', 'locked out', 'failed (422)', 'error', 'unknown credential', 'verification failed'].map(x => M.auditResult(x)).join()
  === 'ok,denied,denied,denied,failed,failed,denied,denied');
check('a failed sign-in is denied whatever its wording', M.auditResult('something new', 'login') === 'denied' && M.auditResult('ok', 'login') === 'ok');
console.log(`${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
