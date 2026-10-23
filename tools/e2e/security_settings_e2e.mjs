// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Settings → Security, on mocked APIs:
// nothing is written to the gateway it runs against.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node tools/e2e/security_settings_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const NOW = Date.now() / 1000;
const fx = {
  sec: { allowlist: [], allow_writes: true, allow_nonlan_http_devices: false, your_ip: '192.0.2.50' },
  ui: { tls_enabled: false, tls_cert: '', tls_key: '', auth_enabled: true, auth_username: 'admin', viewer_username: 'viewer',
        operator_username: '', canonical_url: '', nodes_url: '/nodes/', lockout_threshold: 5, lockout_minutes: 5 },
  passkeys: { passkeys: [{ id: 'p1', label: 'Laptop', user: 'admin', role: 'admin', rp_id: 'gw.lan', created: NOW - 86400 }] },
  tokens: { tokens: [{ name: 'ems', role: 'admin', created: NOW - 50 * 86400, last_used: NOW - 30 },
                     { name: 'forgotten', role: 'viewer', created: NOW - 45 * 86400, last_used: null }] },
  audit: { entries: [
    ...Array.from({ length: 40 }, (_, i) => ({ ts: NOW - i * 600, user: i % 2 ? 'admin' : 'ems', ip: '192.0.2.' + (i % 5), action: 'PUT /api/devices/x', target: '', status: 'ok', detail: '{}' })),
    { ts: NOW - 100, user: 'anon', ip: '198.51.100.7', action: 'login', target: '', status: 'invalid credentials', detail: '' },
    { ts: NOW - 200, user: 'admin', ip: '192.0.2.1', action: 'POST /api/devices', target: '', status: 'failed (422)', detail: '' },
  ] },
};
const posts = [];
const auditUrls = [];

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(width, height) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.setDefaultTimeout(15000);
  page.dlg = await watchDialogs(page);
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push(m.text()); });
  const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route(u => ['/api/config/security', '/api/config/ui-security', '/api/auth/passkeys', '/api/auth/service-tokens', '/api/audit']
    .includes(new URL(u).pathname), route => {
    const path = new URL(route.request().url()).pathname, m = route.request().method();
    if (m === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      posts.push([path, body]);
      if (path === '/api/config/security') Object.assign(fx.sec, body);
      if (path === '/api/config/ui-security') Object.assign(fx.ui, body);
      return json(route, { ok: true });
    }
    if (path === '/api/audit') {
      auditUrls.push(route.request().url());
      const user = new URL(route.request().url()).searchParams.get('user');
      return json(route, { entries: fx.audit.entries.filter(e => !user || e.user === user) });
    }
    return json(route, { '/api/config/security': fx.sec, '/api/config/ui-security': fx.ui, '/api/auth/passkeys': fx.passkeys,
                         '/api/auth/service-tokens': fx.tokens, '/api/audit': fx.audit }[path]);
  });
  await page.addInitScript(() => { try { localStorage.removeItem('mbg-security-tab'); } catch (e) {} });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(() => window.app.openSettingsTab('security'));
  await page.waitForSelector('#secPills .st-pill');
  await page.waitForTimeout(800);
  return { page, errs };
}
const shot = async (page, name) => { if (OUT) await page.screenshot({ path: `${OUT}/${name}.png` }); };

try {
  const { page, errs } = await open(1440, 950);
  await shot(page, 'sec-network');
  const issues = await page.$$eval('#secAttention .st-issue', els => els.map(e => e.dataset.key));
  check('attention: login without HTTPS, and the forgotten token', issues.join() === 'no-https,unused-tokens', issues.join());
  check('pills: five, writes armed is not a warning', (await page.locator('#secPills .st-pill').count()) === 5
    && !(await page.locator('#secPills [data-key="writes"]').getAttribute('class')).includes('st-warn'));
  check('opens on Network with the save bar', await page.locator('#secPanel-network').isVisible() && await page.locator('#secSaveBar').isVisible());
  check('your IP is shown', /192\.0\.2\.50/.test(await page.locator('#allowlistYourIp').innerText()));

  // an Open button leads to its tab; passkeys and tokens have no save bar
  await page.click('#secAttention .st-issue[data-key="unused-tokens"] button');
  check('Open → Passkeys & tokens', await page.locator('#secPanel-access').isVisible() && !(await page.locator('#secSaveBar').isVisible()));
  check('passkeys and tokens listed', (await page.locator('#passkeyTableBody tr').count()) === 1 && (await page.locator('#tokenTableBody tr').count()) === 2);
  await shot(page, 'sec-access');

  // dirty flag, then Save clears it
  await page.click('#secTab-accounts');
  check('accounts: the login fields', await page.locator('#cfgAuthUser').isVisible() && await page.locator('#cfgLockoutN').isVisible());
  check('clean at first', !(await page.locator('#secDirty').isVisible()));
  await page.fill('#cfgLockoutN', '7');
  check('a change shows "Unsaved changes"', await page.locator('#secDirty').isVisible());
  page.dlg.answers.push(false);
  await page.click('[data-page="dashboard"]');
  check('leaving with unsaved changes asks first, and stays', await page.locator('#secPanel-accounts').isVisible());
  await page.click('[data-action="saveSecurity"]');
  await page.waitForTimeout(1200);
  check('Save sends the change', posts.some(([p, b]) => p === '/api/config/ui-security' && b.lockout_threshold === 7));
  check('Save clears the flag', !(await page.locator('#secDirty').isVisible()));

  // turning HTTPS on removes the warning after Save
  await page.click('#secTab-network');
  await page.check('#cfgUiTls');
  await page.click('[data-action="saveSecurity"]');
  await page.waitForTimeout(1200);
  check('HTTPS on over a plain-HTTP page: not active yet, said so', (await page.$$eval('#secAttention .st-issue', els => els.map(e => e.dataset.key))).join() === 'https-pending,unused-tokens');
  // a language switch keeps unsaved edits and the tab; leaving drops them
  await page.fill('#cfgCanonicalUrl', 'https://typo.example');
  await page.evaluate(() => window.app.setLanguage('ro'));
  await page.waitForTimeout(800);
  check('language switch keeps the unsaved edit and the tab', await page.inputValue('#cfgCanonicalUrl') === 'https://typo.example'
    && await page.locator('#secPanel-network').isVisible() && await page.locator('#secDirty').isVisible());
  await page.evaluate(() => window.app.setLanguage('en'));
  await page.waitForTimeout(500);
  page.dlg.answers.push(true);
  await page.click('[data-page="dashboard"]');
  await page.waitForTimeout(300);
  check('leaving drops the edit: the form shows what is stored', await page.inputValue('#cfgCanonicalUrl') === '');
  await page.evaluate(() => window.app.openSettingsTab('security'));
  await page.waitForTimeout(800);
  check('back on Security: clean, on the remembered tab', !(await page.locator('#secDirty').isVisible()) && await page.locator('#secPanel-network').isVisible());

  // writes
  await page.click('#secTab-writes');
  check('writes: the switches', await page.locator('#cfgAllowWrites').isVisible() && await page.locator('#cfgAllowNonlanHttp').isVisible());

  // audit: scrolls, filters
  await page.click('#secTab-audit');
  await page.waitForTimeout(800);
  await shot(page, 'sec-audit');
  const rows = () => page.locator('#auditTableBody tr').count();
  check('audit: all 42 rows, no save bar', (await rows()) === 42 && !(await page.locator('#secSaveBar').isVisible()));
  check('audit: scrolls inside its box', await page.evaluate(() => { const b = document.querySelector('#secPanel-audit .st-evscroll'); return b.scrollHeight > b.clientHeight + 50; }));
  await page.click('#auditResult [data-v="denied"]');
  check('audit: denied only', (await rows()) === 1 && /invalid/.test(await page.locator('#auditTableBody').innerText()));
  await page.click('#auditResult [data-v="failed"]');
  check('audit: failed only', (await rows()) === 1);
  await page.click('#auditResult [data-v="all"]');
  await page.selectOption('#auditUser', 'ems');
  await page.waitForTimeout(500);
  check('audit: the user filter asks the server', auditUrls.some(u => /user=ems/.test(u)));
  check('audit: by user', (await rows()) === 20 && (await page.locator('#auditN-all').innerText()) === '20');
  check('audit: the user picker keeps everyone', (await page.locator('#auditUser option').count()) === 4);

  // the tab is remembered; login off shows the note on Passkeys & tokens
  fx.ui.auth_enabled = false;
  await page.evaluate(() => window.app.loadSecurity());
  await page.waitForTimeout(500);
  await page.evaluate(() => window.app.securityTab('access'));
  check('login off: passkeys and tokens still shown, with the note', await page.locator('#secAccessNote').isVisible() && await page.locator('#tokenTableBody').isVisible());
  check('login off: the warning', (await page.$$eval('#secAttention .st-issue', els => els.map(e => e.dataset.key))).includes('login-off'));
  await page.evaluate(() => window.app.setLanguage('ro'));
  await page.waitForTimeout(800);
  check('Romanian', /Conturi/.test(await page.locator('#secTab-accounts').innerText()));
  await page.evaluate(() => window.app.setLanguage('en'));
  check('no page errors (desktop)', errs.length === 0, errs.join(' | '));
  await page.close();

  const ph = await open(390, 844);
  let ok = true;
  for (const tab of ['network', 'accounts', 'access', 'writes', 'audit']) {
    await ph.page.evaluate(t => window.app.securityTab(t), tab);
    await ph.page.waitForTimeout(300);
    const wide = await ph.page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (wide) { ok = false; console.log('  horizontal scroll on', tab); }
  }
  await shot(ph.page, 'sec-phone');
  check('phone: no horizontal page scroll on any tab', ok);
  check('no page errors (phone)', ph.errs.length === 0, ph.errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
