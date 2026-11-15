/* UX-10: the overview must keep live updates stable under the operator's focus.
 * renderFleet now patches keyed groups/rows in place via _morph instead of
 * replacing the whole subtree (ui/js/app-fleet.js), so a focused pin/row survives
 * a redraw and row DOM nodes are reused rather than recreated.
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node fleet_stable_dom_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.waitForSelector('#fleetView .fleet-row', { timeout: 20000 });

  // 1) row DOM nodes are REUSED across a redraw (morph patch, not innerHTML wipe)
  const reused = await page.evaluate(() => {
    const first = document.querySelector('#fleetView .fleet-row');
    first.__mbgTag = 'keep-me';
    app.renderFleet();
    const again = document.querySelector('#fleetView .fleet-row');
    return again && again.__mbgTag === 'keep-me';
  });
  check('a row node survives a redraw (patched in place, not recreated)', reused);

  // 2) focus on a pin is preserved across a redraw
  const focusKept = await page.evaluate(() => {
    const pin = document.querySelector('#fleetView .fleet-pin');
    if (!pin) return 'no-pin';
    pin.focus();
    const before = document.activeElement === pin;
    app.renderFleet();
    const after = document.activeElement === document.querySelector('#fleetView .fleet-pin');
    return before && after;
  });
  check('a focused pin keeps focus through a redraw', focusKept === true, focusKept);

  // 3) a no-op redraw does nothing (morph short-circuits on identical html)
  const noop = await page.evaluate(() => {
    const view = document.getElementById('fleetView');
    const n1 = view.querySelectorAll('.fleet-row').length;
    app.renderFleet(); app.renderFleet();
    return { n1, n2: view.querySelectorAll('.fleet-row').length };
  });
  check('row count stable across repeated redraws', noop.n1 === noop.n2 && noop.n1 > 0, noop);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
