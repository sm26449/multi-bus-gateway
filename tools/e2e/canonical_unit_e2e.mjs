// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// A canonical name promises a unit and a measurement: picking one in the
// template editor fills Unit and Category while they are empty (or still
// what the dictionary put there), never what the user typed; a different
// unit stays flagged, with a one-click "→ Wh".
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node canonical_unit_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const TPL = 'e2e_canon_unit';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  return { status: r.status, body };
};

await api(`/api/device-templates/${TPL}`, { method: 'DELETE' });
const made = await api('/api/device-templates', { method: 'POST', body: JSON.stringify({ device_template: {
  id: TPL, name: 'E2E canonical units', vendor: 'E2E', protocol: { transports: ['tcp'], byte_order: 'big' },
  poll_groups: { normal: { interval: 2 } },
  registers: [
    { address: 0, name: 'reg_a', unit: '', data_type: 'uint16', category: '', poll_group: 'normal' },
    { address: 1, name: 'reg_b', unit: 'kWh', data_type: 'uint32', category: 'energy', poll_group: 'normal' },
  ] } }) });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const row = i => `#devTplBody tr[data-idx="${i}"]`;
async function set(i, f, v) {
  const l = page.locator(`${row(i)} input[data-f="${f}"]`);
  await l.fill(v); await l.dispatchEvent('change');
}
const val = (i, f) => page.locator(`${row(i)} input[data-f="${f}"]`).inputValue();

try {
  check('a template with two plain rows (API)', made.status === 200, JSON.stringify(made.body).slice(0, 200));
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="templates"]');
  await page.click(`[data-tm-edit="${TPL}"]`);
  await page.waitForSelector('#devTplModal.active');

  await set(0, 'name', 'voltage_l1_n');
  // an existing row gets the unit only: a category is the InfluxDB
  // measurement, and filling it moved the row's history (FINDINGS C2-M1);
  // a new row gets both (rules_templates_step3_e2e)
  check('a canonical name fills the unit of an existing row, never its category', await val(0, 'unit') === 'V' && await val(0, 'category') === '',
    `${await val(0, 'unit')} / ${await val(0, 'category')}`);
  check('no fix button where the unit is right', await page.locator('#devTplBody .tpl-unitfix:visible').count() === 0);
  await set(0, 'name', 'current_l1');
  check('another canonical name replaces what the dictionary filled', await val(0, 'unit') === 'A' && await val(0, 'category') === '',
    `${await val(0, 'unit')} / ${await val(0, 'category')}`);
  await set(0, 'name', 'my_own_thing');
  check('a non-canonical name takes back what the dictionary filled', await val(0, 'unit') === '' && await val(0, 'category') === '',
    `${await val(0, 'unit')} / ${await val(0, 'category')}`);
  await set(0, 'unit', 'mA');
  await set(0, 'name', 'current_l1');
  check('a unit typed by hand stays, and is flagged', await val(0, 'unit') === 'mA'
    && await page.locator(`${row(0)} input[data-f="unit"].tpl-noncanon`).count() === 1);

  await set(1, 'name', 'energy_active_import');
  const fix = page.locator(`${row(1)} .tpl-unitfix`);
  check('a native kWh under an energy name stays kWh and offers → Wh', await val(1, 'unit') === 'kWh'
    && await fix.isVisible() && (await fix.textContent()).includes('Wh'));
  check('the category the user had stays', await val(1, 'category') === 'energy');
  await fix.click();
  check('→ Wh sets the canonical unit and clears the flag', await val(1, 'unit') === 'Wh' && !(await fix.isVisible())
    && await page.locator(`${row(1)} input[data-f="unit"].tpl-noncanon`).count() === 0);

  await page.click('#devTplModal [data-action="saveDevTemplate"], #devTplModal button:has-text("Save")');
  await page.waitForTimeout(1200);
  const saved = (await api(`/api/device-templates/${TPL}`)).body;
  const regs = (saved?.device_template || saved?.template || saved || {}).registers || [];
  const r0 = regs.find(r => r.address === 0) || {}, r1 = regs.find(r => r.address === 1) || {};
  check('saved as shown, with no auto marks in the file', r0.name === 'current_l1' && r0.unit === 'mA' && r1.unit === 'Wh'
    && !JSON.stringify(regs).includes('auto'), JSON.stringify(regs).slice(0, 300));
  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await api(`/api/device-templates/${TPL}`, { method: 'DELETE' });
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
