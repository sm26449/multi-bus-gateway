/* CSP smoke (3.82.0): the page runs with script-src 'self' — no inline
 * handlers, no inline script — and every action is a data-action attribute
 * dispatched by app-core.js. This checks, in a real browser: login under the
 * strict CSP, every top-level page, modal open/close through data-action,
 * change/input dispatch (data-on), data-with-value, the audit export link and
 * that the console shows no CSP violation, unknown data-action or page error.
 *
 *   MBG_URL=http://localhost:18090 MBG_USER=admin MBG_PASS=… node csp_smoke.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18090';
const USER = process.env.MBG_USER || 'admin';
const PASS = process.env.MBG_PASS || '';
const EXEC = process.env.CHROME_PATH;   // optional: a system/other chromium

const results = [];
const check = (name, ok, extra = '') => { results.push({ name, ok, extra }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`); };

const br = await chromium.launch(EXEC ? { executablePath: EXEC } : {});
const page = await br.newPage({ viewport: { width: 1400, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') errs.push(`[${m.type()}] ${m.text().slice(0, 200)}`); });
page.on('pageerror', e => errs.push(`[pageerror] ${String(e).slice(0, 200)}`));
const requests = [];
page.on('request', r => requests.push(r.url()));

// ---- 1. login shell under the strict CSP ---------------------------------
await page.goto(BASE + '/');
const csp = await page.evaluate(async () => (await fetch('/api/auth/status')).headers.get('content-security-policy') || '');
check('CSP script-src is self only', /script-src 'self'(;|$)/.test(csp) && !csp.includes("'unsafe-inline'; style") && !/script-src[^;]*unsafe-inline/.test(csp), csp);
await page.waitForSelector('#loginUser', { state: 'visible' });
await page.fill('#loginUser', USER);
await page.fill('#loginPass', PASS);
await page.click('#loginBtn');
await page.waitForSelector('#loginOverlay', { state: 'hidden', timeout: 20000 }).catch(() => {});
check('login succeeds', await page.isHidden('#loginOverlay'));
errs.length = 0;   // pre-login 401 noise is expected
const later = page.locator('button:has-text("Later")');
if (await later.count()) await later.first().click().catch(() => {});

// ---- 2. every top-level page ---------------------------------------------
for (const pg of ['dashboard', 'devices', 'templates', 'rules', 'vmeters', 'status', 'config', 'diagnostics']) {
    await page.click(`[data-page="${pg}"]`);
    await page.waitForTimeout(700);
    const active = await page.evaluate(p => !!document.querySelector(`.page.active#${p}Page, .page.active[data-page-id="${p}"], #page-${p}.active, .page.active`), pg);
    check(`page ${pg} renders`, active);
}

// ---- 3. modals through data-action (open + close by delegated closeModal) --
await page.click('[data-page="devices"]'); await page.waitForTimeout(500);
for (const [btn, modal] of [['openPlantWizard', 'plantWizardModal'], ['openDeviceWizard', 'deviceWizardModal']]) {
    await page.locator(`[data-action="${btn}"]`).first().click();
    await page.waitForTimeout(400);
    const open = await page.evaluate(id => document.getElementById(id)?.classList.contains('active'), modal);
    check(`data-action ${btn} opens #${modal}`, !!open);
    await page.locator(`#${modal} [data-action="closeModal"]`).first().click();
    await page.waitForTimeout(300);
    const closed = await page.evaluate(id => !document.getElementById(id)?.classList.contains('active'), modal);
    check(`delegated closeModal closes #${modal}`, closed);
}

// ---- 4. change dispatch (data-on="change") in the rule editor --------------
await page.click('[data-page="rules"]'); await page.waitForTimeout(500);
await page.click('#rulesAddBtn'); await page.waitForTimeout(500);
const kinds = await page.$$eval('#rlKind option', o => o.map(x => x.value));
if (kinds.length > 1) {
    // a step row's delete button: data-action="_removeRow" data-with-el (kind = steps)
    await page.selectOption('#rlKind', 'steps'); await page.waitForTimeout(150);
    const before = await page.$$eval('#rlSteps tbody tr', r => r.length);
    await page.locator('[data-action="_ruleAddStep"]').click(); await page.waitForTimeout(150);
    const mid = await page.$$eval('#rlSteps tbody tr', r => r.length);
    await page.locator('#rlSteps tbody tr [data-action="_removeRow"]').last().click(); await page.waitForTimeout(150);
    const after = await page.$$eval('#rlSteps tbody tr', r => r.length);
    check('_ruleAddStep / _removeRow round-trip', mid === before + 1 && after === before, `${before}/${mid}/${after}`);
    const other = kinds.find(k => k !== 'steps');
    await page.selectOption('#rlKind', other);
    await page.waitForTimeout(200);
    const shown = await page.$$eval('#ruleModal [data-rl-kind]', els => els.filter(e => !e.hidden).map(e => e.dataset.rlKind));
    check('data-on=change reaches _ruleKindChanged', shown.length > 0 && shown.every(k => k === other), JSON.stringify(shown));
    await page.selectOption('#rlOnStale', 'value');
    await page.waitForTimeout(200);
    check('data-with-value passes the select value (on_stale)', await page.evaluate(() => document.getElementById('rlStaleValueWrap')?.hidden === false));
}
await page.locator('#ruleModal [data-action="closeModal"]').first().click().catch(() => {});
await page.waitForTimeout(200);

// ---- 5. input dispatch (data-on="input") + the audit export link -------------
await page.click('[data-page="config"]'); await page.waitForTimeout(500);
const secTab = page.locator('[data-cfgtab="security"]');
if (await secTab.count()) { await secTab.first().click(); await page.waitForTimeout(400); }
const auditInput = page.locator('#auditFilter');
if (await auditInput.count() && await auditInput.first().isVisible()) {
    requests.length = 0;
    await auditInput.first().fill('login');
    await page.waitForTimeout(700);
    check('data-on=input debounced audit reload', requests.some(u => u.includes('/api/audit')), requests.filter(u => u.includes('/api/audit')).slice(0, 2).join(' '));
    const csvLink = page.locator('a[href="/api/audit/export.csv"]').first();
    check('audit export is a plain link (no navigation helper in markup)', await csvLink.count() === 1);
} else {
    check('audit filter visible (skipped input/export checks)', false, 'not visible on this page');
}

// ---- 6. the console: no CSP violation, no dead data-action, no page error ---
const bad = errs.filter(e => /Content Security Policy|Refused to execute|data-action: (unknown method|bad data-args)|pageerror/.test(e));
check('no CSP violation / dead action / page error in console', bad.length === 0, bad.slice(0, 5).join(' | '));
if (errs.length) console.log('console noise:', errs.slice(0, 8).join('\n  '));

await br.close();
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
