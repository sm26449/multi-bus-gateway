/* End-to-end validation of the Fronius endpoint UI (3.40.x, migration F1):
 * endpoints card, endpoint chip on materialized device rows, the endpoint-managed
 * Edit/Outputs panels in the device workspace, the Add/Edit Endpoint dialog,
 * and the datalogger-vs-direct template naming the operator relies on.
 *
 * Run against an EPHEMERAL instance (auth off, TEST-NET hosts):
 *   NODE_PATH=<node_modules with playwright> MBG_URL=http://localhost:8093 \
 *     node fronius_endpoint_e2e.mjs
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

  // ---- 2. endpoint GROUPS in the devices list (units nested underneath) ------
  const groups = page.locator('#devicesList .endpoint-row');
  check('two endpoint groups listed', await groups.count() === 2,
        `count=${await groups.count()}`);
  const meterGroup = page.locator('#devicesList .endpoint-row', { hasText: 'fronius-meter' });
  check('meter group shows the unit census chip',
        await meterGroup.locator('.dev-chip', { hasText: /unit/ }).count() >= 1);
  check('meter group shows the SunSpec meter template',
        (await meterGroup.textContent()).includes('fronius_sunspec_meter'));
  check('group has a chevron (expand affordance)',
        await meterGroup.locator('.endpoint-chevron').count() === 1);
  // collapse → nested container hides; expand back
  await meterGroup.click();
  const unitsBox = page.locator('[data-endpoint-units="fronius-meter"]');
  check('collapse hides the endpoint units', !(await unitsBox.isVisible()));
  await meterGroup.click();
  check('expand shows the endpoint units', await unitsBox.isVisible());

  // ---- 3. nested unit row --------------------------------------------------
  const row240 = unitsBox.locator('.device-row', { hasText: 'fronius-meter-240' });
  check('meter unit row nests under its endpoint group', await row240.count() === 1);
  check('meter unit row has NO delete button',
        await row240.locator('button[title="Delete"], button [class*="trash"]').count() === 0);

  // ---- 4. device workspace: endpoint-managed Edit -----------------------------
  await row240.click();
  await page.waitForSelector('#deviceDetailView [data-dpanel="overview"]');
  await page.screenshot({ path: SHOT('f1-02-overview') });
  await page.click('#deviceWsTabs [data-dtab="edit"]');
  const editPanel = page.locator('[data-dpanel="edit"]');
  // UNIFIED workspace: same layout as any device, endpoint-owned fields locked
  check('edit tab shows the endpoint-owned banner with Edit endpoint',
        await editPanel.locator('button:has-text("Edit endpoint")').count() >= 1);
  check('edit tab shows the STANDARD connection form',
        await editPanel.locator('#ddvHost').count() === 1);
  check('connection fields are locked (endpoint-owned)',
        await editPanel.locator('#ddvHost').isDisabled()
        && await editPanel.locator('#ddvUnit').isDisabled());
  check('unit id shows 240', await editPanel.locator('#ddvUnit').inputValue() === '240');
  check('template select locked with the meter template selected',
        await editPanel.locator('#ddvTemplate').isDisabled()
        && (await editPanel.locator('#ddvTemplate option:checked').textContent()).includes('via datalogger'));
  check('edit tab has NO Save & Apply (endpoint owns the definition)',
        await editPanel.locator('button:has-text("Save & Apply")').count() === 0);
  check('edit tab keeps per-unit poll intervals',
        await editPanel.locator('#ddvPollGroups .ddv-pg').count() >= 1);
  await page.screenshot({ path: SHOT('f1-03-edit-endpoint-managed') });

  // ---- 5. outputs tab: SAME sink cards, endpoint-level toggles locked ---------
  await page.click('#deviceWsTabs [data-dtab="outputs"]');
  const outPanel = page.locator('[data-dpanel="outputs"]');
  check('outputs shows the unit topic prefix',
        (await outPanel.locator('#ddvTopic').inputValue()).includes('fronius-meter-240')
        || (await outPanel.locator('#ddvTopic').inputValue()).includes('meter/240'));
  check('outputs shows the shadow bucket',
        (await outPanel.locator('#ddvBucket').inputValue()) === 'fronius_shadow');
  check('sink toggles present but endpoint-locked',
        await outPanel.locator('#ddvMqttEnabled').isDisabled()
        && await outPanel.locator('#ddvInfluxEnabled').isDisabled());
  check('write-protection card present on the unit',
        await outPanel.locator('#ddvWriteLock').count() === 1);
  check('outputs has NO Save & Apply for endpoint units',
        await outPanel.locator('button:has-text("Save & Apply")').count() === 0);
  await page.screenshot({ path: SHOT('f1-04-outputs-endpoint') });

  // ---- 6. measurements tab reachable + populated ---------------------------
  const measTab = page.locator('#deviceWsTabs [data-dtab="measurements"]');
  check('measurements tab shows the 48 seeded registers',
        (await measTab.textContent()).includes('48'));

  // ---- 7. Edit Endpoint dialog from the unit ----------------------------------
  await editPanel.locator('button:has-text("Edit endpoint"), button:has-text("Editează")')
      .first().waitFor({ state: 'attached' }).catch(() => {});
  await page.click('#deviceWsTabs [data-dtab="edit"]');
  await editPanel.locator('button', { hasText: /Edit endpoint|Editează/ }).first().click();
  await page.waitForSelector('#endpointModal.active, #endpointModal[style*="display"]',
                             { state: 'attached' });
  const idInput = page.locator('#plId');
  check('endpoint dialog opens prefilled', await idInput.inputValue() === 'fronius-meter');
  check('endpoint id locked on edit', await idInput.isDisabled());
  check('endpoint dialog shows unit list', (await page.locator('#plUnits').inputValue()).includes('240'));
  check('endpoint dialog preselects the meter template',
        await page.locator('#plTemplate').inputValue() === 'fronius_sunspec_meter');
  await page.screenshot({ path: SHOT('f1-05-endpoint-dialog') });
  await page.click('#endpointModal .modal-close');

  // ---- 8. console hygiene (before the intentional-422 API probe) -----------
  check('zero console errors', consoleErrors.length === 0,
        consoleErrors.slice(0, 3).join(' | '));

  // ---- 9. API guard sanity (the UI story matches the API) ------------------
  // deliberately triggers a 422, which chromium logs as a console error —
  // hence it runs AFTER the console-hygiene check
  const del = await page.evaluate(async () =>
    (await fetch('/api/devices/fronius-meter-240', { method: 'DELETE' })).status);
  check('API refuses deleting an endpoint unit (422)', del === 422);
} catch (e) {
  check('script completed', false, String(e).slice(0, 300));
  await page.screenshot({ path: SHOT('f1-99-error') }).catch(() => {});
}

await browser.close();
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
