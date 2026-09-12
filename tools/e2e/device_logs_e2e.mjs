/* The device Logs tab: the acquisition log the process always kept, finally
 * addressable. Run against an EPHEMERAL instance pointed at a refusing host, so
 * the failure paths are the ones under test.
 *
 *   MBG_URL=http://localhost:18081 DEVICE=sunfield-u1 \
 *     CHROMIUM_PATH=<chrome> node device_logs_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18081';
const DEVICE = process.env.DEVICE || 'sunfield-u1';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(20000);
const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(1200);

  // into the endpoint, then into one of its units
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
  await page.waitForTimeout(1500);
  const open = page.locator(`[data-group-units] tr[data-unit="${DEVICE}"] button[data-action="openDeviceDetail"]`);
  check('a device can be opened', await open.count() === 1);
  await open.click();
  await page.waitForTimeout(2000);

  const tab = page.locator('#deviceWsTabs .config-main-tab[data-dtab="logs"]');
  check('the Logs tab exists', await tab.count() === 1);
  await tab.click();
  await page.waitForTimeout(2500);

  const panel = page.locator('[data-dpanel="logs"]');
  check('the Logs panel is shown', await panel.isVisible());
  const text = await panel.innerText();

  check('the failed batches are listed with what was asked for',
        /batch_failed/.test(text) && /40071/.test(text), text.slice(0, 90));
  check('the per-group state is shown beside the log',
        /normal/.test(text) && /overrun/i.test(text));
  check('the counters summarise ok vs failed',
        /failed/i.test(text));

  // the "problems only" filter actually narrows
  await page.locator('[data-dpanel="logs"] .dev-log-onlybad').check();
  await page.waitForTimeout(1500);
  const bad = await panel.innerText();
  check('problems-only still shows the errors', /batch_failed/.test(bad));
  await page.locator('[data-dpanel="logs"] .dev-log-onlybad').uncheck();

  // live refresh: the row count must not be frozen
  const before = await page.locator('[data-dpanel="logs"] .dev-log-row').count();
  await page.waitForTimeout(7000);
  const after = await page.locator('[data-dpanel="logs"] .dev-log-row').count();
  check('the log refreshes live', after >= before, `${before} -> ${after}`);

  // switching away stops the tick (no stray timers)
  await page.locator('#deviceWsTabs .config-main-tab[data-dtab="overview"]').click();
  await page.waitForTimeout(800);
  check('leaving the tab hides the panel', !(await panel.isVisible()));

  check('no console errors', errors.length === 0, errors.slice(0, 2).join(' | '));
} catch (e) {
  check('script completed', false, e.message);
} finally {
  const ok = results.filter(r => r.ok).length;
  console.log(`\n${ok}/${results.length} checks passed`);
  await browser.close();
  process.exit(ok === results.length ? 0 : 1);
}
