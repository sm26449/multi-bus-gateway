// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// A page left open across a deploy runs the old code (FINDINGS PROD-6): it
// says so with a bar, never reloads by itself, and "Later" holds until the
// next build. The old tab is played by rewriting the build the page loaded.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node build_banner_e2e.mjs
// Any fixture; read-only (nothing is written).
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const errs = [];
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', e => errs.push(String(e)));
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later0 = page.locator('button:has-text("Later")');
  if (await later0.count()) await later0.first().click().catch(() => {});
  const bar = page.locator('#buildBanner');

  const mine = await page.locator('meta[name="mbg-build"]').getAttribute('content');
  const srv = (await (await page.request.get(BASE + '/api/status')).json()).ui_build;
  check('the page carries its build, the same the gateway serves', /^[0-9a-f]{12}$/.test(mine || '') && mine === srv, `${mine} / ${srv}`);
  await page.waitForTimeout(6000);                        // one status poll
  check('a current page shows no bar', await bar.isHidden());

  // the tab now holds an older build
  await page.evaluate(() => document.querySelector('meta[name="mbg-build"]').setAttribute('content', '000000000000'));
  await bar.waitFor({ state: 'visible', timeout: 12000 }).catch(() => {});
  check('an older page is told the gateway was updated', await bar.isVisible());
  check('it is a polite status, not an alert', await bar.getAttribute('role') === 'status');
  const text = await bar.innerText().catch(() => '');
  check('it says that a reload loses what is being edited', /reload/i.test(text) && /(lose|pierde)/i.test(text), text);
  check('it offers Reload and Later as buttons', await bar.locator('button').count() === 2);
  const urlBefore = page.url();
  await page.waitForTimeout(6000);
  check('it never reloads by itself', page.url() === urlBefore && await page.evaluate(() => document.querySelector('meta[name="mbg-build"]').content) === '000000000000');

  await bar.locator('button').nth(1).click();
  check('Later hides it', await bar.isHidden());
  check('focus lands on the page, not lost on the body', await page.evaluate(() => document.activeElement !== document.body));
  await page.waitForTimeout(6000);
  check('Later holds until the next build', await bar.isHidden());

  // the gateway is updated once more: the page is asked again
  await page.route('**/api/status', async route => {
    const r = await route.fetch(); const j = await r.json();
    await route.fulfill({ response: r, json: { ...j, ui_build: 'ffffffffffff' } });
  });
  await bar.waitFor({ state: 'visible', timeout: 12000 }).catch(() => {});
  check('a newer build after Later asks again', await bar.isVisible());
  await page.unroute('**/api/status');
  // a reload is the real fix: the bar is gone and the build matches again
  await Promise.all([page.waitForLoadState('load'), bar.locator('button').first().click()]);
  await page.waitForLoadState('networkidle');
  check('Reload loads the served build', await page.locator('meta[name="mbg-build"]').getAttribute('content') === srv);
  await page.waitForTimeout(6000);
  check('and the bar is gone', await page.locator('#buildBanner').isHidden());

  // phone width: the bar wraps, the page does not scroll sideways
  await page.setViewportSize({ width: 380, height: 800 });
  await page.evaluate(() => document.querySelector('meta[name="mbg-build"]').setAttribute('content', '222222222222'));
  await page.locator('#buildBanner').waitFor({ state: 'visible', timeout: 12000 }).catch(() => {});
  check('on a phone it fits the width', await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e));
} finally {
  await browser.close();
}
const ok = results.filter(r => r.ok).length;
console.log(`${ok}/${results.length} passed`);
process.exit(ok === results.length ? 0 : 1);
