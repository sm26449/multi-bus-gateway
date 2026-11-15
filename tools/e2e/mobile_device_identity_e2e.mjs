/* UX-06: a device's human-readable name must stay visible on a phone. The device
 * header reserves the whole first line for the name (ui/css/config.css ≤600px) and
 * wraps the action group to its own row, instead of letting a nonshrinking action
 * group squeeze the name to nothing.
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node mobile_device_identity_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(() => app.openDeviceDetail('vfd1')); await page.waitForTimeout(1500);

  const name = page.locator('.ddv-head .ddv-name').first();
  check('the device name is rendered', await name.count() >= 1);
  check('the device name is visible', await name.isVisible());
  const nb = await name.boundingBox();
  check('the name has real width (not squeezed out)', nb && nb.width >= 100, nb && Math.round(nb.width));
  check('the name text is non-empty', (await name.innerText()).trim().length > 0, (await name.innerText()).trim());

  // the action group is on its own row, BELOW the name line (not sharing it)
  const h2 = page.locator('.ddv-head h2').first();
  const acts = page.locator('.ddv-head .header-actions').first();
  const hb = await h2.boundingBox(); const ab = await acts.boundingBox();
  check('the actions wrap to their own row below the name', hb && ab && ab.y >= hb.y + hb.height - 4,
        { nameBottom: hb && Math.round(hb.y + hb.height), actionsTop: ab && Math.round(ab.y) });

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(r => !r.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
