/* The Measurements tab of a unit read several ways.
 *
 * It filed every Solar API field under "other", showed invented numeric
 * addresses for JSON paths, a dash for every live value, MONITORED/REALTIME
 * badges, a delete cross on template rows and five buttons in a row. This
 * drives what replaced that: one classification, "where" as an operator reads
 * it, ticking in place with an immediate save, a lock instead of a delete on
 * template rows, and one accessible menu.
 *
 *   MBG_URL=http://localhost:18087 ENDPOINT=sunfield UNIT=sunfield-u1 \
 *     CHROMIUM_PATH=<chrome> node measurements_e2e.mjs
 *
 * Uses the installation-safety fixture (solar_api http + sunspec tcp on units
 * 1 and 2). It ticks and unticks one field and puts the selection back.
 */
import { chromium } from 'playwright';
import { openDeviceTab, openDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://localhost:18087';
const UNIT = process.env.UNIT || 'sunfield-u1';
// the sunspec source reads at its template's `normal` rhythm (the fixture
// sets none): asked, never written down, so a template change cannot stale it
const SS = (await (await fetch(BASE + '/api/device-templates/fronius_sunspec_inverter')).json()).device_template.poll_groups.normal.interval;
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const sel = async (src) => (await fetch(`${BASE}/api/registers/selected?device=${UNIT}&source=${src}`)).json();

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const errs = [];
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const original = (await sel('solar_api')).registers;

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(1200);
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
  await page.waitForTimeout(1500);
  await page.locator(`[data-group-units] tr[data-unit="${UNIT}"] button[data-action="openDeviceDetail"]`).first().click();
  await page.waitForTimeout(1500);
  await openDeviceTab(page, 'measurements');
  await page.waitForTimeout(3000);
  // the tab is the view (L3.3): what is read, with no catalog and no Save;
  // ticking and the catalog are the editor's (⋯ → Measurements…)
  check('the Measurements tab shows what is read: no catalog, no Save, no ticks',
        await page.locator('#selectedRegistersList').isVisible()
        && !(await page.locator('#deviceRegTabs [data-regtab="available"]').isVisible())
        && !(await page.locator('#saveRegistersBtn').isVisible())
        && !(await page.locator('#selectedRegistersList td.reg-tick input').first().isVisible().catch(() => false)));
  await openDeviceEditor(page, 'measurements');
  await page.waitForTimeout(2000);

  // ---- 1. what is read comes first, and the picker states the census ----------
  check('Selected is the tab you land on',
        await page.locator('#deviceRegTabs .config-main-tab.active[data-regtab="selected"]').count() === 1);
  await page.waitForFunction(() => /ticked/.test(document.getElementById('regSourceBar')?.innerText || ''), null, { timeout: 10000 });
  const bar = (await page.locator('#regSourceBar').innerText()).replace(/\s+/g, ' ');
  check('the picker says how each source reads and how much is ticked',
        /solar_api · HTTP · every 5 s · \d+\/\d+ ticked/.test(bar) && new RegExp(`sunspec · Modbus TCP · every ${SS} s`).test(bar), bar.slice(0, 120));
  const n = original.length;
  check('the Selected count matches the picker', (await page.locator('#regTabSelCount').innerText()) === `(${n})`,
        await page.locator('#regTabSelCount').innerText());

  // ---- 2. one classification; "where" as an operator reads it ---------------
  const selList = page.locator('#selectedRegistersList');
  const selText = await selList.innerText();
  check('rows are grouped by what they measure, not "other"',
        await selList.locator('tr.reg-group').count() >= 1 && !/\bOTHER\b/i.test(await selList.locator('tr.reg-group').first().innerText()));
  check('a JSON source shows its paths, not invented addresses', /Body\.Data\./.test(selText) && !/^\s*[1-7]\s*$/m.test(selText));
  check('the interval is the source\'s, in words', /every 5 s/.test(selText));
  check('template rows carry a lock, not a delete',
        await selList.locator('.reg-lock').count() >= 1 && await selList.locator('.btn-action.remove').count() === 0);
  check('no MONITORED / REALTIME badges', !/MONITORED|REALTIME/.test(selText));

  // ---- 3. ticking in place is staged: the full editor's one Save writes it
  // (ADR-0032 §6, L3.11b) -------------------------------------------------------
  const firstRow = selList.locator('tbody tr[data-address]').first();
  const firstName = await firstRow.locator('.reg-name').innerText();
  await firstRow.locator('input[type=checkbox]').click();
  await page.waitForTimeout(1000);
  check('an untick is not written before Save', (await sel('solar_api')).registers.length === n);
  await page.click('#ddvEditSave');
  await page.waitForTimeout(2000);
  const afterUntick = (await sel('solar_api')).registers;
  check('Save writes the untick', afterUntick.length === n - 1 && !afterUntick.some(r => r.name === firstName), `${n} → ${afterUntick.length}`);
  check('and the Selected count follows', (await page.locator('#regTabSelCount').innerText()) === `(${n - 1})`);
  await page.locator('#deviceRegTabs .config-main-tab[data-regtab="available"]').click();
  await page.waitForTimeout(800);
  const avail = page.locator('#registersTableBody');
  check('All available groups by category too', await avail.locator('tr.reg-group').count() >= 3);
  check('Query is not offered for a JSON source', !(await page.locator('#queryRegisterBtn').isVisible()));
  const row = avail.locator('tr[data-address]').filter({ hasText: firstName }).first();
  check('the unticked field is unticked in All available', !(await row.locator('input[type=checkbox]').isChecked()));
  await row.locator('input[type=checkbox]').click();
  await page.waitForTimeout(800);
  await page.click('#ddvEditSave');
  await page.waitForTimeout(2000);
  const afterTick = (await sel('solar_api')).registers;
  check('a tick is written by Save', afterTick.length === n && afterTick.some(r => r.name === firstName), `${afterTick.length}`);
  check('the sunspec map was never touched', (await sel('sunspec')).registers.length > 10);

  // ---- 4. the source switch changes "where" and the census ---------------------
  await page.locator('#regSourceBar button:has-text("sunspec")').click();
  await page.waitForTimeout(2500);
  const sunText = await avail.innerText();
  check('a Modbus source shows addresses with their type', /4007\d\s+uint16/.test(sunText), sunText.slice(0, 80));
  check('Query is offered for a Modbus source', await page.locator('#queryRegisterBtn').isVisible());
  check('the Selected count is the sunspec map\'s now', /\((\d+)\)/.test(await page.locator('#regTabSelCount').innerText()) && (await page.locator('#regTabSelCount').innerText()) !== `(${n})`);

  // ---- 5. no menu inside the tab: the map's tools are the editor's (ADR-0032 §4) ----
  check('no "More" menu in the Measurements editor', !(await page.locator('#regMenuBtn').count()) && !(await page.locator('#regMenu').count()));
  const tools = await page.locator('#deviceRegistersView .reg-tools button:visible').allInnerTexts();
  check('the editor holds Import CSV, Upload map, Download map, Raw JSON', ['Import CSV', 'Upload map', 'Download map', 'Raw JSON']
        .every(w => tools.some(x => x.includes(w))), tools.join(' | '));
  check('Write is not here (it is ⋯ → Write a value…)', !(await page.locator('#writeRegBtn').count()));

  check('no page errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 300));
} finally {
  try {   // put the fixture's selection back exactly
    await fetch(`${BASE}/api/registers/selected?device=${UNIT}&source=solar_api`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(original) });
  } catch (e) { /* best effort */ }
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}
