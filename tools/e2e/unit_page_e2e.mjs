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

const BASE = process.env.MBG_URL || 'http://localhost:18087';
const EP = process.env.ENDPOINT || 'sunfield';
const UNIT = process.env.UNIT || 'sunfield-u1';
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
  check('the unit row names its sources and rhythms', /solar_api HTTP 5 s/.test(rowText) && /sunspec Modbus TCP 20 s/.test(rowText), rowText.slice(0, 90));
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
  check('Overview states every source with protocol and rhythm', /solar_api[\s\S]*HTTP[\s\S]*every 5 s/.test(ov) && /sunspec[\s\S]*Modbus TCP 127\.0\.0\.1:9 · unit 1[\s\S]*every 20 s/.test(ov));
  check('Overview does not show a Modbus source with an empty host', !/—:502|Modbus TCP\s*\n?—/.test(ov));
  check('the footer states this unit\'s rhythm', /read every[\s\S]*solar_api/.test(await page.locator('#pollGroupsStatus').innerText()));

  // ---- 4. the Edit tab is "Read via": no form for a connection that is not -----
  const tab = page.locator('#deviceWsTabs [data-dtab="edit"]');
  check('the tab is named Read via', /Read via/.test(await tab.innerText()));
  await tab.click(); await page.waitForTimeout(600);
  const rv = page.locator('[data-dpanel="edit"]');
  check('no host / port / timeout fields', await rv.locator('#ddvHost, #ddvPort, #ddvTimeout, #ddvTemplate').count() === 0);
  check('no Save intervals, no Test connection', !/Save intervals|Test connection/.test(await rv.innerText()));
  check('both sources listed with template and interval',
        await rv.locator('[data-src-row="solar_api"]').count() === 1 && await rv.locator('[data-src-row="sunspec"]').count() === 1
        && /fronius_solar_api_inverter/.test(await rv.innerText()) && /20 s/.test(await rv.innerText()));
  check('the name is the one editable thing and has a label', await rv.locator('label[for="ddvName"]').count() === 1 && await rv.locator('#ddvName').count() === 1);

  // rename round-trip
  await rv.locator('#ddvName').fill('East inverter');
  await rv.locator('button[data-action="saveUnitName"]').click();
  await page.waitForTimeout(2500);
  const api = await (await fetch(`${BASE}/api/endpoints/${EP}`)).json();
  check('the rename landed on the installation', api.units.find(u => u.device_id === UNIT).name === 'East inverter');
  check('and every source survived it', api.groups[0].sources.map(s => s.id).join(',') === 'solar_api,sunspec');
  check('the crumb shows the new name', (await h2.innerText()).includes('East inverter'));

  // ---- 5. Outputs point to the installation, not to a destructive dialog -------
  await page.click('[data-dtab="outputs"]'); await page.waitForTimeout(600);
  const outs = page.locator('[data-dpanel="outputs"]');
  check('Outputs say routing is set on the installation', /set on its installation/.test(await outs.innerText()));
  check('and offer Open installation, not Edit endpoint', await outs.locator('button:has-text("Open installation")').count() === 1 && !/Edit endpoint/.test(await outs.innerText()));

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
