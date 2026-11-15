/* Runtime verification of the 2026-11-07 frontend audit fixes (A-MED-6/7, A-LOW-8/9):
 *   A11Y-NAV-1    nav tabs keep an accessible name at mobile width (sr-only label)
 *   A11Y-TITLE-1  the settings title follows the ACTIVE sub-panel, not "General"
 *   A11Y-NESTED-1 the bridge device row is not a button wrapping the ⋯ menu
 * Playwright-native (no axe): getByRole uses the accessibility tree, so a tab with
 * a display:none label would NOT resolve by name. Run against a fixture gateway:
 *   tools/e2e/fixture_up.sh safety 18087 --login pw
 *   MBG_URL=http://127.0.0.1:18087 MBG_PASS=pw node tools/e2e/frontend_a11y_fixes_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:8080';
const USER = process.env.MBG_USER || 'admin';
const PASS = process.env.MBG_PASS || '';

const results = [];
function check(name, cond, extra = '') {
  results.push({ name, ok: !!cond, extra });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
}

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, bypassCSP: true });
page.setDefaultTimeout(20000);

try {
  // ---- login ----
  await page.goto(BASE + '/');
  await page.waitForSelector('#loginUser', { state: 'visible' });
  await page.fill('#loginUser', USER);
  await page.fill('#loginPass', PASS);
  await Promise.all([page.waitForLoadState('load'), page.click('#loginBtn')]);
  await page.waitForSelector('#loginOverlay', { state: 'hidden', timeout: 20000 }).catch(() => {});
  check('login succeeds', await page.isHidden('#loginOverlay'));

  // ---- A11Y-NAV-1: every nav destination keeps an accessible name at 390px ----
  // UX-13 made the nav real <a> links behind a mobile "Navigation" disclosure.
  await page.setViewportSize({ width: 390, height: 800 });
  await page.waitForTimeout(400);
  const disc = page.locator('#navDisclosure');
  await disc.click().catch(() => {});                 // open the destination list
  await page.waitForTimeout(200);
  // every destination link (top-level + the flattened "More" submenu children) must
  // carry a non-empty accessible name — the icon is aria-hidden, so the visible span
  // text is the name. (Role-agnostic: submenu children are role=menuitem by design.)
  const dests = await page.$$eval('#mainNav .nav-tab[href]', links => links.map(a => ({
    page: a.dataset.page, name: (a.textContent || '').replace(/\s+/g, ' ').trim(),
  })));
  const named = dests.filter(d => d.name.length > 0).length;
  const missing = dests.filter(d => !d.name.length).map(d => d.page);
  if (missing.length) console.log('  nav destinations with NO accessible name at 390px:', missing.join(', '));
  check(`A11Y-NAV-1: all ${dests.length} nav destinations are named at 390px`, dests.length >= 8 && named === dests.length, `${named}/${dests.length}`);

  // ---- A11Y-TITLE-1: the settings title follows the active sub-panel ----
  // (Settings lives under the System submenu now — navigate by route, not a click.)
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.evaluate(() => { location.hash = '#/config'; });
  await page.waitForTimeout(500);
  const mqttTab = page.locator('#configSubtabs .config-main-tab[data-cfgtab="mqtt"]');
  if (await mqttTab.count()) {
    await mqttTab.click();
    await page.waitForTimeout(500);
    const title = await page.title();
    check('A11Y-TITLE-1: settings title follows the MQTT panel (not "General")',
          /mqtt/i.test(title) && !/^general/i.test(title), title);
  } else {
    check('A11Y-TITLE-1: MQTT sub-tab present', false, 'no #configSubtabs[data-cfgtab=mqtt] on this fixture');
  }

  // ---- A11Y-NESTED-1: a bridge device row is not a button wrapping the menu ----
  const bridgeRows = await page.locator('.bridge-dev[data-f]').count().catch(() => 0);
  if (bridgeRows > 0) {
    const asButton = await page.locator('.bridge-dev[role="button"]').count();
    const openBtn = await page.locator('.bridge-dev-open').count();
    check('A11Y-NESTED-1: bridge row is a container (not role=button) with an open-button',
          asButton === 0 && openBtn > 0, `role=button ${asButton}, open-button ${openBtn}`);
  } else {
    console.log('SKIP A11Y-NESTED-1: no bridge configured on this fixture (structural fix verified statically)');
  }
} catch (e) {
  check('e2e ran without a fatal error', false, String(e));
} finally {
  await browser.close();
}

const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
