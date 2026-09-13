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

const BASE = process.env.MBG_URL || 'http://localhost:18087';
const UNIT = process.env.UNIT || 'sunfield-u1';
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
  await page.locator('#deviceWsTabs .config-main-tab[data-dtab="measurements"]').click();
  await page.waitForTimeout(3000);

  // ---- 1. what is read comes first, and the picker states the census ----------
  check('Selected is the tab you land on',
        await page.locator('#deviceRegTabs .config-main-tab.active[data-regtab="selected"]').count() === 1);
  await page.waitForFunction(() => /ticked/.test(document.getElementById('regSourceBar')?.innerText || ''), null, { timeout: 10000 });
  const bar = (await page.locator('#regSourceBar').innerText()).replace(/\s+/g, ' ');
  check('the picker says how each source reads and how much is ticked',
        /solar_api · HTTP · every 5 s · \d+\/\d+ ticked/.test(bar) && /sunspec · Modbus TCP · every 20 s/.test(bar), bar.slice(0, 120));
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

  // ---- 3. ticking in place saves at once ---------------------------------------
  const firstRow = selList.locator('tbody tr[data-address]').first();
  const firstName = await firstRow.locator('.reg-name').innerText();
  await firstRow.locator('input[type=checkbox]').click();
  await page.waitForTimeout(2000);
  const afterUntick = (await sel('solar_api')).registers;
  check('an untick is saved immediately', afterUntick.length === n - 1 && !afterUntick.some(r => r.name === firstName), `${n} → ${afterUntick.length}`);
  check('and the Selected count follows', (await page.locator('#regTabSelCount').innerText()) === `(${n - 1})`);
  await page.locator('#deviceRegTabs .config-main-tab[data-regtab="available"]').click();
  await page.waitForTimeout(800);
  const avail = page.locator('#registersTableBody');
  check('All available groups by category too', await avail.locator('tr.reg-group').count() >= 3);
  check('Query is not offered for a JSON source', !(await page.locator('#queryRegisterBtn').isVisible()));
  const row = avail.locator('tr[data-address]').filter({ hasText: firstName }).first();
  check('the unticked field is unticked in All available', !(await row.locator('input[type=checkbox]').isChecked()));
  await row.locator('input[type=checkbox]').click();
  await page.waitForTimeout(2000);
  const afterTick = (await sel('solar_api')).registers;
  check('a tick is saved immediately', afterTick.length === n && afterTick.some(r => r.name === firstName), `${afterTick.length}`);
  check('the sunspec map was never touched', (await sel('sunspec')).registers.length > 10);

  // ---- 4. the source switch changes "where" and the census ---------------------
  await page.locator('#regSourceBar button:has-text("sunspec")').click();
  await page.waitForTimeout(2500);
  const sunText = await avail.innerText();
  check('a Modbus source shows addresses with their type', /4007\d\s+uint16/.test(sunText), sunText.slice(0, 80));
  check('Query is offered for a Modbus source', await page.locator('#queryRegisterBtn').isVisible());
  check('the Selected count is the sunspec map\'s now', /\((\d+)\)/.test(await page.locator('#regTabSelCount').innerText()) && (await page.locator('#regTabSelCount').innerText()) !== `(${n})`);

  // ---- 5. one menu, keyboard-reachable -------------------------------------------
  const menuBtn = page.locator('#regMenuBtn');
  check('the toolbar is one menu button plus Save',
        await page.locator('#deviceRegistersView .header-actions > button, #deviceRegistersView .header-actions > .reg-menu').count() === 2);
  await menuBtn.click(); await page.waitForTimeout(300);
  check('the menu opens and says so', (await menuBtn.getAttribute('aria-expanded')) === 'true' && await page.locator('#regMenu:visible').count() === 1);
  check('the menu holds import, export, raw and write', await page.locator('#regMenu [role=menuitem]:visible').count() >= 4);
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  check('Escape closes it', (await menuBtn.getAttribute('aria-expanded')) === 'false');
  await page.locator('#regSourceBar button:has-text("solar_api")').click(); await page.waitForTimeout(2000);
  await menuBtn.click(); await page.waitForTimeout(300);
  check('Write is not offered for a JSON source', !(await page.locator('#writeRegBtn').isVisible()));
  await page.keyboard.press('Escape');

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
