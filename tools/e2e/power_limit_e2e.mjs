/* The operator's hand on the power-limit action: the Limit… button on the
 * inverter card, the dialog, and a result in words per unit. Against the
 * safety fixture (hosts that refuse, writes off) the honest outcome is a
 * refusal — which is exactly what must be shown, never a silent nothing.
 *
 *   MBG_URL=http://localhost:18087 ENDPOINT=sunfield CHROMIUM_PATH=<chrome> node power_limit_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://localhost:18087';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
// a 403 from the action is the expected answer on this fixture (writes off), not a page error
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/403/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
try {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]'); await page.waitForTimeout(1000);
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click(); await page.waitForTimeout(2000);
  const inv = page.locator('[data-group="inverters"]');
  check('the inverter card offers Limit…', await inv.locator('button[data-action="openPowerLimitModal"]').count() === 1);
  check('the meter card does not', await page.locator('[data-group="grid"] button[data-action="openPowerLimitModal"]').count() === 0);
  check('the unit table has a Limit column', /LIMIT/i.test(await inv.locator('thead').innerText()));
  await inv.locator('button[data-action="openPowerLimitModal"]').click(); await page.waitForTimeout(500);
  const m = page.locator('#endpointModal');
  check('the dialog asks limit, revert, ramp and scope, all labelled',
        (await m.locator('label[for="plmPct"], label[for="plmRevert"], label[for="plmRamp"], label[for="plmScope"]').count()) === 4);
  check('revert defaults to 600 s', (await m.locator('#plmRevert').inputValue()) === '600');
  check('the scope offers every unit of the group', await m.locator('#plmScope option').count() === 3);
  await m.locator('#plmPct').fill('60');
  page.once('dialog', d => d.accept());
  await m.locator('[data-endpoint-save]').click();
  await page.waitForTimeout(3000);
  const fb = (await m.locator('#endpointFeedback').innerText()) + ' ' + (await m.locator('#plmOut').innerText());
  check('with writes off the refusal is said in words', /disabled|refused|failed|write/i.test(fb), fb.slice(0, 120));
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
