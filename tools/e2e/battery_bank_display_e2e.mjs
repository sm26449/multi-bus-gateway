/* End-to-end checks for template-driven presentation (3.86.0) on a battery
 * bank: the installation page, unit page, dashboard, fleet and history read
 * WHAT to show from the template's `display` block and categories, totals
 * carry their source field's label and unit, "Update from template" refreshes
 * a seeded unit, and a viewer sees no control it cannot use.
 *
 * Needs an EPHEMERAL instance (auth off) with a fake two-pack bus on a PTY:
 *   docker cp tools/e2e/tap_feeder.py <ctr>:/tmp/
 *   docker exec -d -u <app uid> -e TAP_UNITS=2,3 <ctr> python /tmp/tap_feeder.py
 *   cd tools/e2e && MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node battery_bank_display_e2e.mjs
 * CTR (the container) lets the script damage a seeded label to prove the refresh.
 */
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const PORT = process.env.TAP_PORT || '/tmp/ttyTAPE2E';
const CTR = process.env.CTR || '';
const ID = 'bank-disp-e2e';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

await api(`/api/endpoints/${ID}`, { method: 'DELETE' });
const created = await api('/api/endpoints', { method: 'POST', body: JSON.stringify({
  id: ID, name: 'Bank display E2E', template: 'seplos_bms_v3_rtu_tap', enabled: true,
  connection: { protocol: 'rtu_tap', serial_port: PORT, baudrate: 9600 },
  units: [2, 3], influxdb: { enabled: false }, mqtt: { topic_prefix: 'e2e/disp/${unit_id}' },
}) });
check('endpoint created', created.status === 200);

let p = {};
for (let i = 0; i < 25; i++) {
  p = (await api(`/api/endpoints/${ID}`)).body;
  const g = (p.groups || [])[0] || {};
  if ((g.headline_items || []).length && (g.units || []).every(u => (u.live || {}).power != null)) break;
  await sleep(1000);
}
const g = (p.groups || [])[0] || {};
// ---- 1. API: presentation comes from the template --------------------------
check('group says what its units are', g.unit_label_plural === 'Battery packs' && g.icon === 'battery-half',
  `${g.unit_label_plural}/${g.icon}`);
check('glance columns from the template', JSON.stringify(g.glance) === JSON.stringify(['power', 'soc', 'pack_voltage', 'max_cell_temp', 'status']));
const u0 = (g.units || [])[0] || {};
check('a unit row carries power and SOC', u0.live && u0.live.power != null && u0.live.soc != null, JSON.stringify(u0.live));
check('row labels come from the unit\'s registers', ((p.fields || {}).power || {}).label === 'Power (− charging)',
  JSON.stringify((p.fields || {}).power));
const am = g.aggregate_fields || {};
check('a declared total inherits label and unit', am.pack_average_soc && am.pack_average_soc.unit === '%'
  && /SOC \(average\)|State of charge/.test(am.pack_average_soc.label), JSON.stringify(am.pack_average_soc));
check('energy totals not doubled as counters', !('energy_remaining' in (g.aggregates || {})) && !('energy_to_full' in (g.aggregates || {})));
check('no AC "Total active power" twin on a bank', !('power_active_total' in (g.aggregates || {})));
check('headline from the template', (p.headline_items || [])[0]?.label === 'Battery power'
  && (p.headline_items || [])[0]?.hint, JSON.stringify((p.headline_items || [])[0]));
check('order follows the declaration', (g.aggregate_order || []).length > 5);
const sel = (await api(`/api/registers/selected?device=${u0.device_id}`)).body.registers || [];
const cell = sel.find(r => r.name === 'cell_1') || {};
check('template categories reach the register list', cell.category === 'cells' && cell.category_label === 'Cells',
  `${cell.category}/${cell.category_label}`);
const hist = ((await api(`/api/history/registers?device=${ID}`)).body.registers || []).map(r => r.name);
check('installation history offers its declared totals', hist.includes('pack_average_soc') && hist.includes('pack_total_power'));
const fleet = (await api('/api/fleet')).body;
const card = ((fleet.endpoints || []).find(e => e.id === ID) || {}).card || {};
check('fleet card shows the headline metric', card.field === 'pack_total_power' && card.unit === 'W', JSON.stringify(card));
const frow = (fleet.devices || []).find(d => d.id === u0.device_id) || {};
check('fleet row heroes from the template', (frow.hero || []).map(h => h.name).join(',') === 'soc,power,status',
  (frow.hero || []).map(h => h.name).join(','));

// ---- 2. Update from template ------------------------------------------------
if (CTR) {
  const f = `/app/config/devices/${u0.device_id}/selected_registers.json`;
  execFileSync('docker', ['exec', CTR, 'sed', '-i', 's/"Max discharge current (BMS limit)"/"MaxDisCurt"/', f]);
  const before = execFileSync('docker', ['exec', CTR, 'grep', '-c', 'MaxDisCurt', f]).toString().trim();
  const rr = await api(`/api/endpoints/${ID}/refresh-from-template`, { method: 'POST' });
  const after = execFileSync('docker', ['exec', CTR, 'sh', '-c', `grep -c 'Max discharge current (BMS limit)' ${f}`]).toString().trim();
  check('refresh restores a stale seeded label', before === '1' && rr.status === 200 && after === '1',
    `before=${before} status=${rr.status} after=${after} rows=${JSON.stringify((rr.body.units || []).map(u => u.registers))}`);
  await sleep(4000);
}

// ---- 3. The pages -----------------------------------------------------------
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
try {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.evaluate(id => window.app.openEndpointDetail(id), ID);
  await page.waitForSelector(`[data-group-units]`);
  await page.waitForTimeout(1500);
  const head = await page.innerText('#plHeadline');
  check('page headline: battery power with its sign hint', /Battery power/.test(head) && /charging/.test(head), head.replace(/\n/g, ' | ').slice(0, 120));
  const th = await page.locator('[data-group] thead').first().innerText();
  check('unit table columns: power and SOC', /Power \(− charging\)/i.test(th) && /SOC/i.test(th), th.replace(/\s+/g, ' '));
  const row = await page.locator('[data-group-units] tr').first().innerText();
  check('unit row shows values, not dashes', /W/.test(row) && /%/.test(row), row.replace(/\s+/g, ' '));
  const gh = await page.locator('[data-group] h3').first().innerText();
  check('group header names the units', /Battery packs/.test(gh), gh);
  const totals = await page.locator('[data-group-totals]').first().innerText();
  check('totals grid speaks labels and units', /%/.test(totals) && !/pack_average_soc/.test(totals), totals.slice(0, 120).replace(/\n/g, ' | '));
  // viewer: nothing it could not use
  await page.evaluate(() => document.body.classList.add('role-viewer'));
  const visibleAdmin = await page.locator('[data-admin]:visible').count();
  check('a viewer sees no admin control on the page', visibleAdmin === 0, String(visibleAdmin));
  await page.evaluate(() => document.body.classList.remove('role-viewer'));
  // unit page
  await page.evaluate(id => window.app.openDeviceDetail(id), u0.device_id);
  await page.waitForTimeout(2500);
  const ov = await page.innerText('#mainContent');
  check('unit page has no false "no values" note', !/No values yet/.test(ov));
  check('unit page groups by template category', /BATTERY|Battery/.test(ov) && !/\nPACK\n/.test(ov));
  // dashboard
  await page.click('[data-page="dashboard"]');
  await page.evaluate(id => window.app.openFleetDevice(id), u0.device_id);
  await page.waitForTimeout(2000);
  const secs = await page.locator('details.dev-section summary').allInnerTexts();
  check('dashboard sections follow the template (Battery, Cells…)', secs.some(s => /Battery/.test(s)) && secs.some(s => /Cells/.test(s)),
    secs.map(s => s.trim()).join(' / '));
  check('poll group shown as a hint, not a status pill', await page.locator('.widget-card .badge.poll-normal, .widget-card .badge.poll-tap').count() === 0);
  // mobile endpoint table
  await page.setViewportSize({ width: 390, height: 844 });
  await page.click('[data-page="devices"]');
  await page.evaluate(id => window.app.openEndpointDetail(id), ID);
  await page.waitForTimeout(1500);
  const overflow = await page.evaluate(() => {
    const t = document.querySelector('[data-group] table'); const w = t && t.closest('div');
    return t ? t.scrollWidth - w.clientWidth : -1;
  });
  check('phone: unit table fits without side-scrolling', overflow <= 4, `overflow ${overflow}px`);
  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 300));
} finally {
  await api(`/api/endpoints/${ID}`, { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
