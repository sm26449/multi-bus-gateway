/* The page of a unit that belongs to an installation.
 *
 * It used to describe a Modbus connection that did not exist (empty host,
 * :502, timeout 3, no template) for a unit read over HTTP every two seconds,
 * offered Save intervals (a silent no-op) and Test connection on it, and its
 * back arrow lost the installation. This drives what replaced that.
 *
 *   MBG_URL=http://localhost:18087 ENDPOINT=sunfield UNIT=sunfield-u1 \
 *     CHROMIUM_PATH=<chrome> node unit_page_e2e.mjs
 *
 * Config it expects: the installation_safety fixture — `inverters` (units 1,2)
 * read over solar_api (http) AND sunspec (tcp), on hosts that REFUSE.
 */
import { chromium } from 'playwright';
import { openDeviceTab, editorSection, saveDeviceEditor, closeDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://localhost:18087';
const EP = process.env.ENDPOINT || 'sunfield';
const UNIT = process.env.UNIT || 'sunfield-u1';
// the sunspec source reads at its template's `normal` rhythm (the fixture
// sets none): asked, never written down, so a template change cannot stale it
const SS = (await (await fetch(BASE + '/api/device-templates/fronius_sunspec_inverter')).json()).device_template.poll_groups.normal.interval;
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const errs = [];
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(1200);

  // ---- 1. the list row says how the unit is read, not the fallback ----------
  const row = page.locator(`.device-row[data-args*="${UNIT}"]`).first();
  const rowText = await row.innerText();
  // the device's one line (the header's, L3.3): read via <source> (<protocol> <every>) + …
  check('the unit row names its sources and rhythms', /solar_api \(HTTP 5 s\)/.test(rowText) && new RegExp(`sunspec \\(Modbus TCP ${SS} s\\)`).test(rowText), rowText.slice(0, 120));
  check('the unit row does not describe a bare TCP :9', !/TCP · 127|:9 · unit/.test(rowText));
  const epRow = await page.locator('.endpoint-row').first().innerText();
  check('the installation row says what it holds and how it is read', /Inverters/.test(epRow) && /Read via/.test(epRow), epRow.slice(0, 90));

  // ---- 2. into the unit from its installation --------------------------------
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
  await page.waitForTimeout(1500);
  await page.locator(`[data-group-units] tr[data-unit="${UNIT}"] button[data-action="openDeviceDetail"]`).first().click();
  await page.waitForTimeout(2000);
  const h2 = page.locator('#deviceDetailView .section-header h2');
  check('a breadcrumb with three levels', await h2.locator('nav a').count() === 2 && (await h2.innerText()).includes('›'));
  check('the crumb names the installation, not its id alone', (await h2.locator('nav a').nth(1).innerText()).includes('Sunfield PV'));
  check('an Open installation button', await page.locator('#deviceDetailView button:has-text("Open installation")').count() >= 1);

  // ---- 3. Overview: truth about how it is read --------------------------------
  const ov = await page.locator('[data-dpanel="overview"]').innerText();
  check('Overview states every source with protocol and rhythm', /solar_api[\s\S]*HTTP[\s\S]*every 5 s/.test(ov) && new RegExp(`sunspec[\\s\\S]*Modbus TCP 127\\.0\\.0\\.1:15502 · unit 1[\\s\\S]*every ${SS} s`).test(ov));
  check('Overview does not show a Modbus source with an empty host', !/—:502|Modbus TCP\s*\n?—/.test(ov));
  check('the footer states this unit\'s rhythm', /read every[\s\S]*solar_api/.test(await page.locator('#pollGroupsStatus').innerText()));

  // ---- 4. the Edit tab is "Read via": no form for a connection that is not -----
  // (L3.3) not a tab any more: the unit's ⋯ → Rename… opens it
  await page.click('#ddvActions');
  check('its ⋯ offers Rename…; Edit connection… is held: the installation sets it (L3.10)',
        await page.locator('#ddvActionsMenu [data-ddv-act="rename"]').isVisible()
        && (await page.getAttribute('#ddvActionsMenu [data-ddv-act="connection"]', 'aria-disabled')) === 'true'
        && /installation/i.test(await page.locator('#ddvActionsMenu [data-ddv-act="connection"] .mbg-menu-why').innerText()));
  await page.keyboard.press('Escape');
  // (L3.11b) Rename… is the Edit device modal: its Identity holds the name;
  // its Connection says "set on the installation" and lists the sources
  await openDeviceTab(page, 'connection'); await page.waitForTimeout(600);
  await editorSection(page, 'connection');
  const rv = page.locator('#devEditPanel-connection');
  check('no host / port / timeout fields', await page.locator('#devEditModal #ddvHost, #devEditModal #ddvPort, #devEditModal #ddvTimeout, #devEditModal #ddvTemplate').count() === 0);
  check('no Save intervals, no Test connection', !/Save intervals|Test connection/.test(await rv.innerText()));
  check('both sources listed with template and interval',
        await rv.locator('[data-src-row="solar_api"]').count() === 1 && await rv.locator('[data-src-row="sunspec"]').count() === 1
        && /fronius_solar_api_inverter/.test(await rv.innerText()) && new RegExp(`\\b${SS} s`).test(await rv.innerText()));
  check('its connection: set on the installation, with the way there', /set on the installation/i.test(await rv.innerText())
        && await rv.locator('[data-de-way="installation"]').count() === 1);
  await editorSection(page, 'identity');
  const idp = page.locator('#devEditPanel-identity');
  check('the name is editable and has a label', await idp.locator('label[for="ddvName"]').count() === 1 && await idp.locator('#ddvName').count() === 1);

  // rename round-trip
  await idp.locator('#ddvName').fill('East inverter');
  await saveDeviceEditor(page);
  await page.waitForTimeout(1500);
  const api = await (await fetch(`${BASE}/api/endpoints/${EP}`)).json();
  check('the rename landed on the installation', api.units.find(u => u.device_id === UNIT).name === 'East inverter');
  check('and every source survived it', api.groups[0].sources.map(s => s.id).join(',') === 'solar_api,sunspec');
  check('the crumb shows the new name', (await h2.innerText()).includes('East inverter'));

  // ---- 5. Outputs: the installation's, said so, with the way there --------------
  await openDeviceTab(page, 'outputs'); await page.waitForTimeout(600);
  const outs = page.locator('#devEditPanel-outputs');
  check('Outputs say they are the installation\'s, for every unit', /every unit/.test(await outs.innerText()));
  check('and offer Open installation, not Edit endpoint', await outs.locator('[data-de-way="installation"]').count() >= 1 && !/Edit endpoint/.test(await outs.innerText()));
  await closeDeviceEditor(page);

  // ---- 6. back goes to the installation --------------------------------------
  await page.locator('#deviceDetailView .section-header h2 button[aria-label]').first().click();
  await page.waitForTimeout(1500);
  check('the back arrow returns to the installation', await page.locator('[data-endpoint-page]').count() === 1);
  check('and the footer is the gateway\'s again', !/read every/.test(await page.locator('#pollGroupsStatus').innerText()));

  // ---- 7. Status page: the sources' protocols -------------------------------
  await page.click('[data-page="status"]'); await page.waitForTimeout(2500);
  const st = await page.locator('#statusPage, [data-page-content="status"], body').first().innerText();
  check('Status lists the unit as http + tcp', /http \+ tcp/.test(st));

  check('no page errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 300));
} finally {
  try {
    const p = await (await fetch(`${BASE}/api/endpoints/${EP}`)).json();
    const g = p.groups.map(x => ({ id: x.id, role: x.role, enabled: x.enabled, template: x.template,
      units: x.units.map(u => ({ unit_id: u.unit_id, id: u.device_id })) }));
    // put the fixture's default name back
    await fetch(`${BASE}/api/endpoints/${EP}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: EP, name: p.name, enabled: p.enabled, connection: p.connection }) });
    void g;
  } catch (e) { /* best effort */ }
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}
