// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// What the 2026-10-21 UI audit found broken (section B), as a user meets it,
// against the same throwaway gateway as unchanged_save_e2e.mjs (its fixture):
// an MQTT device's Test, the wizard's topic pattern, a rule's Disable /
// Enable, what each role sees, the footer time.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 node tools/e2e/broken_fixes_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();
const rule = async id => (await api('/api/rules')).rules.find(r => r.id === id);

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(role) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.said = () => page.dlg.seen.map(d => [d.title, d.message, ...(d.details || [])].join(' '));
  if (role) {
    // the sandbox runs without auth: the role is what /api/auth/status says
    await page.route('**/api/auth/status', r => r.fulfill({ json: { enabled: true, role, modules: [] } }));
  }
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}

try {
  const page = await open(null);

  // ── Test read on an MQTT device says what its input sees ──
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForTimeout(800);
  const res = await page.evaluate(async () => (await fetch('/api/devices/gx/test', { method: 'POST' })).json());
  check('MQTT device: Test says what its input sees, not "host required"',
        /not listening|connected/.test(res.message) && !/host required/.test(res.message), JSON.stringify(res));

  // ── the wizard shows and uses the configured topic pattern ──
  const pattern = (await api('/api/config/mqtt')).default_topic_pattern;
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForTimeout(400);
  const expect = pattern.replace('{device}', 'em24-hala');
  await page.evaluate(() => { window.app._devWiz.step = 3; window.app._devWizRender(); });
  await page.waitForSelector('#devWizTopic');
  check('wizard: the topic placeholder is the gateway pattern', (await page.getAttribute('#devWizTopic', 'placeholder')) === expect,
        `${await page.getAttribute('#devWizTopic', 'placeholder')} vs ${expect}`);
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));

  // ── Disable / Enable a rule from its card ──
  await page.evaluate(() => window.app.navigateTo('rules'));
  await page.waitForSelector('[data-rule="ov-u1"]');
  const btn = '[data-rule="ov-u1"] [data-action="setRuleEnabled"]';
  check('rule card: a Disable button', (await page.locator(btn).count()) === 1);
  page.dlg.answer = false;
  page.dlg.seen.length = 0;
  await page.click(btn);
  await page.waitForTimeout(500);
  check('rule card: disabling an armed rule asks, names the safe values; "no" keeps it',
        page.said().some(m => /value=100/.test(m)) && (await rule('ov-u1')).enabled === true, page.said().join(' | '));
  page.dlg.answer = true;
  await page.click(btn);
  await page.waitForTimeout(800);
  check('rule card: "yes" disables it', (await rule('ov-u1')).enabled === false);
  page.dlg.seen.length = 0;
  await page.click(btn);
  await page.waitForTimeout(800);
  // enabling an armed rule is arming it again: a gateway without login
  // refuses it (as it refuses Arm) — the refusal must be said, not swallowed
  const toast = await page.locator('.toast').allInnerTexts().then(x => x.join(' | ')).catch(() => '');
  const enabled = (await rule('ov-u1')).enabled === true;
  check('rule card: enabling an armed rule asks, then enables it or says why not',
        page.said().some(m => /write/i.test(m)) && (enabled || /authentication|API_KEY/i.test(toast)),
        `${page.said().join(' | ')} · ${toast}`);

  // ── the footer time is the browser's own clock ──
  await page.evaluate(() => window.app.navigateTo('dashboard'));
  await page.waitForTimeout(2500);
  const footer = await page.locator('#lastUpdate').innerText();
  const shown = footer.match(/(\d{1,2}):(\d{2}):(\d{2})/);
  const now = new Date();
  const diff = shown ? Math.abs((+shown[1] % 12) * 3600 + +shown[2] * 60 + +shown[3]
                                - ((now.getHours() % 12) * 3600 + now.getMinutes() * 60 + now.getSeconds())) : 1e9;
  check('footer: last update is within a few minutes of now', !shown || diff < 600, `${footer} vs ${now.toLocaleTimeString()}`);
  check('no page errors (admin)', page.errs.length === 0, page.errs.join(' | '));
  check('no native dialog (admin)', page.dlg.native === 0, page.said().join(' | '));
  await page.close();

  // ── what each role sees ──
  for (const role of ['operator', 'viewer']) {
    const p = await open(role);
    await p.evaluate(() => window.app.navigateTo('rules'));
    await p.waitForSelector('#rulesAddBtn', { state: 'attached' });
    check(`${role}: no "Add rule"`, !(await p.locator('#rulesAddBtn').isVisible()));
    await p.evaluate(() => window.app.navigateTo('devices'));
    await p.waitForTimeout(800);
    const test = p.locator('[data-action="testDevice"]').first();
    const sees = (await test.count()) && await test.isVisible();
    check(`${role}: device Test ${role === 'operator' ? 'shown' : 'hidden'}`, role === 'operator' ? sees : !sees);
    check(`${role}: no Delete device`, !(await p.locator('#devicesListView [data-action="deleteDevice"]:visible').count()));
    await p.evaluate(() => window.app.navigateTo('config'));
    await p.waitForTimeout(500);
    check(`${role}: no Settings "Save & Apply"`, !(await p.locator('[data-action="saveAndApply"]:visible, [data-action="saveSecurity"]:visible, [data-action="saveGeneralConfig"]:visible, [data-action="createSnapshot"]:visible').count()));
    // every control by its mark: live actions for the operator, configuration for nobody
    const vis = async sel => {
      const els = await p.locator(sel).all();
      let shown = 0;
      for (const el of els) if (await el.isVisible()) shown++;
      return { total: els.length, shown };
    };
    for (const page of ['devices', 'templates', 'vmeters', 'diagnostics', 'rules']) {
      await p.evaluate(pg => window.app.navigateTo(pg), page);
      await p.waitForTimeout(700);
      const admin = await vis('[data-admin]');
      check(`${role}: ${page} — no configuration control shown`, admin.shown === 0, `${admin.shown}/${admin.total}`);
      const live = await vis('[data-live]');
      if (role === 'viewer') check(`viewer: ${page} — no live action shown`, live.shown === 0, `${live.shown}/${live.total}`);
    }
    await p.evaluate(() => window.app.navigateTo('diagnostics'));
    await p.waitForTimeout(500);
    const probe = await p.locator('#probeBtn').isVisible().catch(() => false);
    check(`${role}: Diagnostics probe ${role === 'operator' ? 'shown' : 'hidden'}`, role === 'operator' ? probe : !probe);
    await p.waitForTimeout(11000);          // the first-run prompt waits ~10 s
    check(`${role}: never the first-run prompt`, !(await p.locator('#firstRunModal.active').count()));
    if (OUT) await p.screenshot({ path: `${OUT}/role-${role}.png` });
    check(`no page errors (${role})`, p.errs.length === 0, p.errs.join(' | '));
    await p.close();
  }
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
