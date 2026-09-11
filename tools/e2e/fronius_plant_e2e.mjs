/* End-to-end validation of the Fronius plant UI (3.40.x, migration F1):
 * plants card, plant chip on materialized device rows, the plant-managed
 * Edit/Outputs panels in the device workspace, the Add/Edit Plant dialog,
 * and the datalogger-vs-direct template naming the operator relies on.
 *
 * Run against an EPHEMERAL instance (auth off, TEST-NET hosts):
 *   NODE_PATH=<node_modules with playwright> MBG_URL=http://localhost:8093 \
 *     node fronius_plant_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:8093';
const SHOT = (n) => `${process.env.SHOTS_DIR || './shots'}/${n}.png`;

const results = [];
function check(name, cond, extra = '') {
  results.push({ name, ok: !!cond, extra });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
}

// CHROMIUM_PATH: point at any installed chromium when the cached build
// doesn't match the playwright package version (version-pinned caches).
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 },
                                     bypassCSP: true });
page.setDefaultTimeout(20000);
const consoleErrors = [];
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', e => consoleErrors.push(String(e)));

async function dismissOnboarding() {
  const later = page.locator('button:has-text("Later")');
  if (await later.count().catch(() => 0)) await later.first().click().catch(() => {});
}

try {
  // ---- 1. devices page ---------------------------------------------------
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  await dismissOnboarding();
  await page.click('[data-page="devices"]');
  await page.waitForSelector('#devicesList .device-row');
  await dismissOnboarding();
  await page.screenshot({ path: SHOT('f1-01-devices') });

  // ---- 2. plants card ----------------------------------------------------
  const plantsCard = page.locator('#plantsCard');
  check('plants card visible', await plantsCard.isVisible());
  const plantRows = page.locator('#plantsList .device-row');
  check('two plants listed', await plantRows.count() === 2,
        `count=${await plantRows.count()}`);
  const meterPlant = page.locator('#plantsList .device-row',
                                  { hasText: 'fronius-meter' });
  check('meter plant shows its unit chip',
        await meterPlant.locator('.dev-chip', { hasText: 'unit' }).count() >= 1);
  check('meter plant shows the SunSpec meter template',
        (await meterPlant.textContent()).includes('fronius_sunspec_meter'));

  // ---- 3. materialized device row -----------------------------------------
  const row240 = page.locator('#devicesList .device-row',
                              { hasText: 'fronius-meter-240' });
  check('meter unit row exists', await row240.count() === 1);
  check('meter unit row carries the plant chip',
        await row240.locator('.dev-chip', { hasText: 'fronius-meter' }).count() >= 1);
  check('meter unit row has NO delete button',
        await row240.locator('button[title="Delete"], button [class*="trash"]').count() === 0);

  // ---- 4. device workspace: plant-managed Edit -----------------------------
  await row240.click();
  await page.waitForSelector('#deviceDetailView [data-dpanel="overview"]');
  await page.screenshot({ path: SHOT('f1-02-overview') });
  await page.click('#deviceWsTabs [data-dtab="edit"]');
  const editPanel = page.locator('[data-dpanel="edit"]');
  check('edit tab shows the Plant unit card',
        await editPanel.locator('h3', { hasText: /Plant unit|Unitate/ }).count() === 1);
  const editText = await editPanel.textContent();
  check('edit tab names the plant', editText.includes('fronius-meter'));
  check('edit tab shows unit 240', /240/.test(editText));
  check('edit tab shows the datalogger meter template name',
        editText.includes('via datalogger'));
  check('edit tab has NO generic connection form',
        await editPanel.locator('#ddvHost').count() === 0);
  check('edit tab has NO Save & Apply (plant owns the definition)',
        await editPanel.locator('button:has-text("Save & Apply")').count() === 0);
  check('edit tab keeps per-unit poll intervals',
        await editPanel.locator('#ddvPollGroups .ddv-pg').count() >= 1);
  await page.screenshot({ path: SHOT('f1-03-edit-plant-managed') });

  // ---- 5. outputs tab: plant routing summary -------------------------------
  await page.click('#deviceWsTabs [data-dtab="outputs"]');
  const outPanel = page.locator('[data-dpanel="outputs"]');
  const outText = await outPanel.textContent();
  check('outputs tab shows the plant topic prefix',
        outText.includes('mbg/fronius/meter/240'));
  check('outputs tab shows the shadow bucket',
        outText.includes('fronius_shadow'));
  check('outputs tab has NO per-device sink toggles',
        await outPanel.locator('#ddvMqttEnabled').count() === 0);
  await page.screenshot({ path: SHOT('f1-04-outputs-plant') });

  // ---- 6. measurements tab reachable + populated ---------------------------
  const measTab = page.locator('#deviceWsTabs [data-dtab="measurements"]');
  check('measurements tab shows the 48 seeded registers',
        (await measTab.textContent()).includes('48'));

  // ---- 7. Edit Plant dialog from the unit ----------------------------------
  await editPanel.locator('button:has-text("Edit plant"), button:has-text("Editează")')
      .first().waitFor({ state: 'attached' }).catch(() => {});
  await page.click('#deviceWsTabs [data-dtab="edit"]');
  await editPanel.locator('button', { hasText: /Edit plant|Editează/ }).first().click();
  await page.waitForSelector('#plantModal.active, #plantModal[style*="display"]',
                             { state: 'attached' });
  const idInput = page.locator('#plId');
  check('plant dialog opens prefilled', await idInput.inputValue() === 'fronius-meter');
  check('plant id locked on edit', await idInput.isDisabled());
  check('plant dialog shows unit list', (await page.locator('#plUnits').inputValue()).includes('240'));
  check('plant dialog preselects the meter template',
        await page.locator('#plTemplate').inputValue() === 'fronius_sunspec_meter');
  await page.screenshot({ path: SHOT('f1-05-plant-dialog') });
  await page.click('#plantModal .modal-close');

  // ---- 8. console hygiene (before the intentional-422 API probe) -----------
  check('zero console errors', consoleErrors.length === 0,
        consoleErrors.slice(0, 3).join(' | '));

  // ---- 9. API guard sanity (the UI story matches the API) ------------------
  // deliberately triggers a 422, which chromium logs as a console error —
  // hence it runs AFTER the console-hygiene check
  const del = await page.evaluate(async () =>
    (await fetch('/api/devices/fronius-meter-240', { method: 'DELETE' })).status);
  check('API refuses deleting a plant unit (422)', del === 422);
} catch (e) {
  check('script completed', false, String(e).slice(0, 300));
  await page.screenshot({ path: SHOT('f1-99-error') }).catch(() => {});
}

await browser.close();
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
