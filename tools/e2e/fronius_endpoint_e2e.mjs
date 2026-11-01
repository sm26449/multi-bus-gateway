/* End-to-end validation of the Fronius endpoint UI (3.40.x, migration F1):
 * the installations in the devices list with their units nested, the
 * endpoint-managed Read via / Outputs panels in the device workspace, the
 * Edit Endpoint dialog, and the datalogger-vs-direct template naming the
 * operator relies on — the inverters read through the datalogger's Solar API
 * (answered by tools/e2e/fake_solar_api.mjs: 4200 W each), the meter directly
 * over SunSpec Modbus (a port that refuses).
 *
 * Against the `fronius` fixture (auth off; the stand-in runs in the gateway's
 * own network namespace, nothing else is reached):
 *   tools/e2e/fixture_up.sh fronius 18094
 *   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18094 SHOTS_DIR=<dir> node tools/e2e/fronius_endpoint_e2e.mjs
 *   docker rm -f mbg-fx-fronius mbg-fx-fronius-solar
 */
import { chromium } from 'playwright';
import { openDeviceTab, editorSection, closeDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18094';
const METER = 'fronius-meter-u240';                 // a unit's id: <installation>-u<unit>
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
  const meterGroup = page.locator('#devicesList .endpoint-row[data-endpoint-toggle="fronius-meter"]');
  const invGroup = page.locator('#devicesList .endpoint-row[data-endpoint-toggle="fronius-inverters"]');
  check('meter group shows the unit census chip',
        await meterGroup.locator('.dev-chip', { hasText: /unit/ }).count() >= 1);
  // the datalogger path is live: the stand-in answers 4200 W per inverter
  await page.waitForFunction(() => /2\/2 answering/.test(
    document.querySelector('[data-endpoint-toggle="fronius-inverters"]')?.innerText || ''), null, { timeout: 30000 }).catch(() => {});
  const invText = await invGroup.innerText();
  check('the datalogger group answers, with its total power', /2\/2 answering/.test(invText) && /8,400 W/.test(invText), invText);
  check('the direct meter group says nobody answers', /0\/1 answering/.test(await meterGroup.innerText()));
  // how each unit is read, in its row: the datalogger over HTTP, the meter over Modbus TCP
  const rowsText = async ep => (await page.locator(`[data-endpoint-units="${ep}"] .device-row`).allInnerTexts()).join(' | ');
  check('inverter units are read over HTTP (the Solar API)', /HTTP/.test(await rowsText('fronius-inverters')));
  check('the meter unit is read over Modbus TCP (SunSpec, direct)', /Modbus TCP/.test(await rowsText('fronius-meter')));
  check('group has a chevron (expand affordance)',
        await meterGroup.locator('.endpoint-chevron').count() === 1);
  // collapse → nested container hides; expand back
  await meterGroup.click();
  const unitsBox = page.locator('[data-endpoint-units="fronius-meter"]');
  check('collapse hides the endpoint units', !(await unitsBox.isVisible()));
  await meterGroup.click();
  check('expand shows the endpoint units', await unitsBox.isVisible());

  // ---- 3. nested unit row --------------------------------------------------
  const row240 = unitsBox.locator('.device-row', { hasText: METER });
  check('meter unit row nests under its endpoint group', await row240.count() === 1);
  check('meter unit row has NO delete button',
        await row240.locator('button[title="Delete"], button [class*="trash"]').count() === 0);

  // ---- 4. device workspace: endpoint-managed Edit -----------------------------
  await row240.click();
  await page.waitForSelector('#deviceDetailView [data-dpanel="overview"]');
  await page.screenshot({ path: SHOT('f1-02-overview') });
  await openDeviceTab(page, 'connection');
  // a unit is edited in the Edit device modal (L3.11b): its Connection says
  // how it is read, set on its installation — no connection form of its own
  // (its address, unit and template are the installation's); its name is
  // its Identity's
  await editorSection(page, 'connection');
  const editPanel = page.locator('#devEditPanel-connection');
  check('the unit tab says it is read through its installation, with a way there',
        await editPanel.locator('button:has-text("Open installation")').count() >= 1);
  check('a unit has no connection form of its own',
        await editPanel.locator('#ddvHost, #ddvUnit, #ddvConn').count() === 0);
  check('its sources are listed (address, template, interval)',
        await editPanel.locator('[data-src-row]').count() >= 1);
  check('the source names the meter template it reads with',
        (await editPanel.locator('[data-src-row]').first().textContent()).includes('fronius'));
  check('its name is the one thing it saves (its Identity; one Save)',
        await page.locator('#devEditPanel-identity #ddvName').count() === 1
        && await page.locator('#devEditModal button:has-text("Save & Apply")').count() === 0);
  await page.screenshot({ path: SHOT('f1-03-edit-endpoint-managed') });
  await closeDeviceEditor(page);

  // ---- 5. outputs tab: the same outputs, endpoint-level switches locked ------
  await openDeviceTab(page, 'outputs');
  const outPanel = page.locator('#devEditPanel-outputs');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  const mqttLocked = await outPanel.locator('#outEnabled').isDisabled();
  check('outputs shows the unit topic prefix',
        (await outPanel.locator('#ddvTopic').inputValue()).includes(METER)
        || (await outPanel.locator('#ddvTopic').inputValue()).includes('meter/240'));
  await page.click('#outTab-influxdb'); await page.waitForTimeout(300);
  check('outputs shows the shadow bucket',
        (await outPanel.locator('#ddvBucket').inputValue()) === 'fronius_shadow');
  check('sink toggles present but endpoint-locked',
        mqttLocked && await outPanel.locator('#outEnabled').isDisabled());
  // L3.3: a pill in the header, switched from ⋯ (the installation's lock)
  check('write protection on the unit: its header pill and ⋯ → Lock/Unlock writes…',
        await page.locator('#ddvFacts .dev-sum-lock').count() === 1 && await page.locator('#ddvActionsMenu #ddvMenuLock').count() === 1);
  check('outputs has NO Save & Apply for endpoint units',
        await outPanel.locator('button:has-text("Save & Apply")').count() === 0);
  await page.screenshot({ path: SHOT('f1-04-outputs-endpoint') });
  await closeDeviceEditor(page);

  // ---- 6. measurements tab reachable + populated ---------------------------
  const measTab = page.locator('#deviceWsTabs [data-dtab="measurements"]');
  check('measurements tab shows the 48 seeded registers',
        (await measTab.textContent()).includes('48'));

  // ---- 7. the installation, from the unit: its dialog and its group --------
  // a unit's Read via tab leads to its installation; the installation dialog
  // owns the name and the outputs, the group card its units and template
  await openDeviceTab(page, 'connection');
  await editorSection(page, 'connection');            // the modal's Connection: "set on the installation"
  await editPanel.locator('button:has-text("Open installation")').first().click();
  await page.waitForSelector('[data-endpoint-page]');
  check('"Open installation" opens the meter\'s installation',
        (await page.locator('[data-endpoint-page]').innerText()).includes('Fronius meter'));
  await page.evaluate(() => window.app.openEndpointModal('fronius-meter'));
  await page.waitForSelector('#endpointModal.active #plId');
  const idInput = page.locator('#plId');
  check('endpoint dialog opens prefilled', await idInput.inputValue() === 'fronius-meter');
  check('endpoint id locked on edit', await idInput.isDisabled());
  await page.screenshot({ path: SHOT('f1-05-endpoint-dialog') });
  await page.evaluate(() => window.app.closeModal('endpointModal'));
  const groupDialog = async ep => {
    await page.evaluate(e => window.app.openGroupModal(e, 'units'), ep);
    await page.waitForSelector('#endpointModal.active #grpUnits');
    const r = { units: await page.inputValue('#grpUnits'), tpl: await page.inputValue('#grpTpl') };
    await page.evaluate(() => window.app.closeModal('endpointModal'));
    return r;
  };
  const mg = await groupDialog('fronius-meter');
  check('the meter group shows its unit', mg.units.includes('240'), mg.units);
  // the naming the operator relies on: direct = SunSpec, datalogger = Solar API
  check('the direct meter group takes the SunSpec meter template',
        mg.tpl === 'fronius_sunspec_meter' || (mg.tpl === '' && (await (await fetch(`${BASE}/api/endpoints/fronius-meter`)).json()).template === 'fronius_sunspec_meter'), mg.tpl);
  await page.evaluate(() => window.app.openEndpointDetail('fronius-inverters'));
  await page.waitForSelector('[data-endpoint-page]');
  const ig = await groupDialog('fronius-inverters');
  check('the datalogger group takes the Solar API inverter template',
        ig.tpl === 'fronius_solar_api_inverter' || (ig.tpl === '' && (await (await fetch(`${BASE}/api/endpoints/fronius-inverters`)).json()).template === 'fronius_solar_api_inverter'), ig.tpl);
  // and its units carry what the datalogger answered
  const v = await (await fetch(`${BASE}/api/values?device=fronius-inverters-u1`)).json();
  const pac = Object.values(v.values || {}).find(x => x.name === 'power_active_total');
  check('a datalogger unit reads the Solar API value (4200 W)', pac?.value === 4200, JSON.stringify(pac));

  // ---- 8. console hygiene (before the intentional-422 API probe) -----------
  check('zero console errors', consoleErrors.length === 0,
        consoleErrors.slice(0, 3).join(' | '));

  // ---- 9. API guard sanity (the UI story matches the API) ------------------
  // deliberately triggers a 422, which chromium logs as a console error —
  // hence it runs AFTER the console-hygiene check
  const del = await page.evaluate(async id =>
    (await fetch(`/api/devices/${id}`, { method: 'DELETE' })).status, METER);
  check('API refuses deleting an endpoint unit (422)', del === 422);
} catch (e) {
  check('script completed', false, String(e).slice(0, 300));
  await page.screenshot({ path: SHOT('f1-99-error') }).catch(() => {});
}

await browser.close();
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
