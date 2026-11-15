/* Dashboard drill-in is a REAL route (#/dashboard/<device>), not a hidden mode flag.
 * Bug fixed: select a device on the dashboard, go to another section, return via the
 * Dashboard tab → the OVERVIEW shows (not the stale device). Back/Forward, refresh and
 * a deep link all land on the exact view. (ui/js/app-router.js + app-core.js + app-fleet.js)
 *
 *   tools/e2e/fixture_up.sh plant 18085
 *   MBG_URL=http://127.0.0.1:18085 CHROMIUM_PATH=<chrome> node dashboard_drilldown_route_e2e.mjs
 */
import { chromium } from 'playwright';
import { openPage } from './lib/nav.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18085';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.waitForSelector('#fleetView .fleet-row', { timeout: 20000 });

  const mode = () => page.evaluate(() => ({
    hash: location.hash,
    overview: !document.getElementById('fleetView').hidden,
    device: !document.getElementById('dashboardGrid').hidden,
  }));
  const firstId = await page.$eval('#fleetView .fleet-row', el => el.dataset.id);

  let m = await mode();
  check('starts on the fleet overview at #/dashboard', /#\/dashboard$/.test(m.hash) && m.overview && !m.device, m);

  // drill into a device → the URL names it
  await page.click('#fleetView .fleet-row'); await page.waitForTimeout(400);
  m = await mode();
  check('selecting a device routes to #/dashboard/<id> and shows the device',
        m.hash === `#/dashboard/${firstId}` && m.device && !m.overview, m);

  // go to another section, then back to the Dashboard TAB → overview, not the stale device
  await openPage(page, 'devices'); await page.waitForTimeout(400);
  check('nav away goes to #/devices', /#\/devices/.test((await mode()).hash));
  await page.click('.nav-tab[data-page="dashboard"]'); await page.waitForTimeout(400);
  m = await mode();
  check('returning via the Dashboard tab shows the OVERVIEW (the bug)', /#\/dashboard$/.test(m.hash) && m.overview && !m.device, m);

  // drill in again, then Back/Forward walk the two views
  await page.click('#fleetView .fleet-row'); await page.waitForTimeout(400);
  check('drilled in again', (await mode()).device);
  await page.goBack(); await page.waitForTimeout(400);
  m = await mode();
  check('browser Back returns to the overview', /#\/dashboard$/.test(m.hash) && m.overview, m);
  // return context: the row we drilled from regains focus
  const focused = await page.evaluate(id => {
    const a = document.activeElement;
    return !!(a && a.closest && a.closest(`#fleetView .fleet-row[data-id="${id}"]`));
  }, firstId);
  check('Back restores focus to the fleet row we drilled from', focused, { firstId });
  await page.goForward(); await page.waitForTimeout(400);
  m = await mode();
  check('browser Forward returns to the device', m.hash === `#/dashboard/${firstId}` && m.device, m);

  // refresh on the device URL → the device is shown (deep-linkable)
  await page.reload(); await page.waitForLoadState('networkidle'); await page.waitForTimeout(800);
  m = await mode();
  check('refresh on #/dashboard/<id> re-opens the device', m.hash === `#/dashboard/${firstId}` && m.device, m);

  // deep-link to a non-existent device → the overview, not a silent other device
  await page.goto(BASE + '/#/dashboard/no-such-device'); await page.waitForLoadState('networkidle'); await page.waitForTimeout(800);
  m = await mode();
  check('a deleted/unknown device in the URL falls back to the overview', m.overview && !m.device, m);

  await ctx.close();
} catch (e) { check('ran', false, String(e).slice(0, 220)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
