/* UX-01: Save & Apply feedback must be HONEST. A configuration save that returns
 * 200 followed by an apply that fails — an apply HTTP error, or the enabled
 * service failing to reconnect — must NOT reach "Saved & applied". fetch does not
 * reject on an HTTP error status, so app-settings.saveAndApply inspects the apply
 * response instead of awaiting-and-ignoring it (ui/js/app-settings.js).
 *
 *   tools/e2e/fixture_up.sh drive 18098        (MBG_FX_NAME=mbg-fx-apply-fb)
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node settings_apply_feedback_e2e.mjs
 *
 * Login off (drive variant). The save + apply calls are intercepted, so nothing
 * in the fixture config is actually mutated.
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

  // ── Unit checks of the pure decision helper (DOM-free, both service keys) ──
  const outcome = (section, enabled, ok, data) =>
    page.evaluate(([s, e, o, d]) => app._applyOutcome(s, e, o, d), [section, enabled, ok, data]);
  check('helper: apply 500 -> not_applied', (await outcome('influx', true, false, { detail: 'boom' })).state === 'not_applied');
  check('helper: apply ok but enabled service false -> not_applied',
        (await outcome('influx', true, true, { results: { influxdb: false } })).state === 'not_applied');
  check('helper: apply ok and enabled service true -> applied',
        (await outcome('mqtt', true, true, { results: { mqtt: true } })).state === 'applied');
  check('helper: a DISABLED service reporting false is NOT a failure',
        (await outcome('influx', false, true, { results: { influxdb: false } })).state === 'applied');
  check('helper: section maps to the right results key (influx -> influxdb)',
        (await outcome('influx', true, true, { results: { influxdb: false, mqtt: true } })).state === 'not_applied');

  // ── Integration: drive the real Save & Apply button with an intercepted apply ──
  let applyScenario = null;   // set per case
  await page.route('**/api/config/influxdb', route => {
    if (route.request().method() !== 'POST') return route.continue();
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) });
  });
  await page.route('**/api/config/apply', route => {
    if (route.request().method() !== 'POST' || !applyScenario) return route.continue();
    route.fulfill({ status: applyScenario.status, contentType: 'application/json', body: JSON.stringify(applyScenario.body) });
  });

  const openInflux = async () => { await page.evaluate(() => app.openSettingsTab('influxdb')); await page.waitForTimeout(900);
    await page.evaluate(() => { const c = document.getElementById('cfgInfluxEnabled'); if (c && !c.checked) { c.checked = true; c.dispatchEvent(new Event('change')); } }); };
  const clickSave = async () => { await page.click('[data-action="saveAndApply"][data-args=\'["influx"]\']'); await page.waitForTimeout(1200); };
  const fb = async () => ({ text: (await page.locator('#influxSaveFeedback').innerText()).trim(),
                            cls: await page.getAttribute('#influxSaveFeedback', 'class') });

  // A) save 200 + apply 500 -> saved-but-not-applied (warn), never success
  applyScenario = { status: 500, body: { detail: 'simulated apply error' } };
  await openInflux(); await clickSave();
  let f = await fb();
  check('A: apply 500 shows saved-but-not-applied, not success',
        /warn/.test(f.cls) && !/ok\b/.test(f.cls) && !/Saved & applied/.test(f.text) && /simulated apply error/.test(f.text), f);

  // B) save 200 + apply 200 with influxdb result false (enabled) -> not applied
  applyScenario = { status: 200, body: { status: 'ok', results: { modbus: true, mqtt: true, influxdb: false } } };
  await openInflux(); await clickSave();
  f = await fb();
  check('B: enabled service that did not reconnect is not reported as success',
        /warn/.test(f.cls) && !/Saved & applied/.test(f.text) && /reconnect/.test(f.text), f);

  // C) save 200 + apply 200 with influxdb result true -> full success
  applyScenario = { status: 200, body: { status: 'ok', results: { modbus: true, mqtt: true, influxdb: true } } };
  await openInflux(); await clickSave();
  f = await fb();
  check('C: a genuine success still shows "Saved & applied"', /\bok\b/.test(f.cls) && /Saved & applied/.test(f.text), f);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(r => !r.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
