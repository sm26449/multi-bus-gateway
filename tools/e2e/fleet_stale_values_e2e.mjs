/* UX-03: a device whose acquisition has stopped shows its hero numbers as
 * LAST-KNOWN readings, not as a current measurement. app._fleetRow marks a
 * down/stale device's metrics with a de-emphasised style, a clock-history icon
 * and a visually-hidden "last known" label (ui/js/app-fleet.js). A fresh (ok)
 * device is untouched. Rendered HTML is asserted directly, so the case is
 * deterministic without forcing a live stale device.
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node fleet_stale_values_e2e.mjs
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

  const row = dev => page.evaluate(d => app._fleetRow(d), dev);
  const hero = [{ name: 'voltage', label: 'Voltage L1', unit: 'V', value: 233.5 }];

  // down + an age + hero readings → last-known treatment
  const down = await row({ id: 'inv1', name: 'Inverter 1', health: 'down', staleness_age_s: 1260,
                           enabled: true, protocol: 'tcp', hero });
  check('down+stale: metrics container marked last-known', /fleet-metrics--last-known/.test(down), down.slice(0, 0));
  check('down+stale: clock-history icon present', /bi-clock-history/.test(down));
  check('down+stale: a visually-hidden "last known" labels the value', /class="sr-only">\s*last known/i.test(down));
  check('down+stale: the last-known value is still shown (not hidden)', /233\.5/.test(down));
  check('down+stale: the age flag still reads No answer · 21m', /No answer/.test(down) && /21m/.test(down));

  // ok (fresh) → no last-known decoration at all
  const ok = await row({ id: 'inv2', name: 'Inverter 2', health: 'ok', staleness_age_s: 3,
                         enabled: true, protocol: 'tcp', hero });
  check('fresh: NOT marked last-known', !/fleet-metrics--last-known/.test(ok));
  check('fresh: no clock-history icon', !/bi-clock-history/.test(ok));
  check('fresh: no "last known" label', !/last known/i.test(ok));

  // down but with NO hero readings → nothing to label as last-known
  const downNoHero = await row({ id: 'inv3', name: 'Inverter 3', health: 'down', staleness_age_s: 600,
                                 enabled: true, protocol: 'tcp', hero: [] });
  check('down with no readings: not marked last-known', !/fleet-metrics--last-known/.test(downNoHero));

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(r => !r.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
