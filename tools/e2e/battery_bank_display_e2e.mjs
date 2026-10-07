/* End-to-end checks for template-driven presentation (3.86.0) on a battery
 * bank: the installation page, unit page, dashboard, fleet and history read
 * WHAT to show from the template's `display` block and categories, totals
 * carry their source field's label and unit, "Update from template" refreshes
 * a seeded unit, and a viewer sees no control it cannot use.
 *
 * Needs an EPHEMERAL instance (auth off) with a fake two-pack bus on a PTY
 * (unit 3 has a weak cell 5 and one active alarm):
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
{
  // a re-created installation finds its old register files (kept on delete):
  // the template's newer calculated fields are added when asked
  const add = await api(`/api/endpoints/${ID}/refresh-from-template?add_new=true`, { method: 'POST' });
  const again = await api(`/api/endpoints/${ID}/refresh-from-template?add_new=true`, { method: 'POST' });
  check('adding the template\'s new fields is idempotent', add.status === 200
    && (again.body.units || []).every(u => u.added === 0),
    JSON.stringify([(add.body.units || []).map(u => u.added), (again.body.units || []).map(u => u.added)]));
  await sleep(4000);
}
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
  // ---- template update: offered only when there is something to bring in --
  check('up to date: no template notice, no update button',
    (await page.locator('#plTplUpdate .tpl-update-notice').count()) === 0
    && (await page.locator('button:has-text("Update from template")').count()) === 0);
  if (CTR) {
    const f = `/app/config/devices/${u0.device_id}/selected_registers.json`;
    execFileSync('docker', ['exec', CTR, 'sed', '-i', 's/"Max discharge current (BMS limit)"/"MaxDisCurt"/', f]);
    await page.waitForSelector('#plTplUpdate .tpl-update-notice', { timeout: 12000 }).catch(() => {});
    const note = await page.locator('#plTplUpdate').innerText().catch(() => '');
    check('an aged unit raises the notice', /template/i.test(note) && /1 change/.test(note), note.replace(/\s+/g, ' '));
    await page.locator('#plTplUpdate button').click();
    await page.waitForSelector('#tplUpdateModal.active .tplu-table');
    const body = await page.innerText('#tplUpdateBody');
    check('the modal shows what changes, before and after',
      /maxdiscurt/.test(body) && /MaxDisCurt/.test(body) && /Max discharge current \(BMS limit\)/.test(body)
      && /1\/2 units/.test(body), body.replace(/\s+/g, ' ').slice(0, 200));
    await page.click('#tplUpdateApply');
    await page.waitForSelector('#tplUpdateModal.active', { state: 'detached', timeout: 15000 }).catch(() => {});
    await page.waitForFunction(() => !document.querySelector('#plTplUpdate .tpl-update-notice'), null, { timeout: 15000 }).catch(() => {});
    check('applied: the notice is gone', (await page.locator('#plTplUpdate .tpl-update-notice').count()) === 0);
    const back = execFileSync('docker', ['exec', CTR, 'sh', '-c', `grep -c 'Max discharge current (BMS limit)' ${f}`]).toString().trim();
    check('applied: the label is back on disk', back === '1', back);
    // the apply restarts the units: wait until they read again
    for (let i = 0; i < 30; i++) {
      const g2 = ((await api(`/api/endpoints/${ID}`)).body.groups || [])[0] || {};
      if ((g2.units || []).length && (g2.units || []).every(u => (u.live || {}).power != null)) break;
      await sleep(1000);
    }
    await page.evaluate(id => window.app.openEndpointDetail(id), ID);
    await page.waitForSelector(`[data-group-units]`);
    await page.waitForTimeout(1500);
  }
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
  // ---- 4. C: alarms the template declares; D: grid, active-only, bitmasks
  await page.setViewportSize({ width: 1440, height: 950 });
  const u3 = (g.units || []).find(u => u.unit_id === 3) || {};
  // the alarm comes from a coil block the bus carries every few seconds:
  // wait for it rather than sample once
  let pe = {}, pu3 = {};
  for (let i = 0; i < 20; i++) {
    pe = (await api(`/api/endpoints/${ID}`)).body;
    pu3 = (pe.units || []).find(u => u.unit_id === 3) || {};
    if ((pu3.alarms || {}).warning) break;
    await sleep(1000);
  }
  check('C: the unit with an active alarm is counted', pe.units_alarming === 1 && (pu3.alarms || {}).warning === 1,
    JSON.stringify({ n: pe.units_alarming, a: pu3.alarms }));
  const fl = ((await api('/api/fleet')).body.devices || []).find(d => d.id === u3.device_id) || {};
  check('C: the fleet counts it too', (fl.alarms || {}).warning >= 1, JSON.stringify(fl.alarms));
  await page.click('[data-page="devices"]');
  await page.evaluate(id => window.app.openEndpointDetail(id), ID);
  await page.waitForTimeout(1500);
  check('C: census says how many units alarm', /alarming/.test(await page.innerText('#plCensus')));
  const r3 = await page.locator(`[data-group-units] tr[data-unit="${u3.device_id}"]`).innerHTML();
  check('C: the unit row carries the alarm pill', /bi-exclamation-triangle/.test(r3));
  await page.evaluate(id => window.app.openDeviceDetail(id), u3.device_id);
  await page.waitForTimeout(2000);
  check('C: the unit page lists the active alarm', /Warning Count: 1/i.test(await page.innerText('#mainContent')));
  await page.click('[data-page="dashboard"]');
  await page.evaluate(id => window.app.openFleetDevice(id), u3.device_id);
  await page.waitForTimeout(2500);
  await page.evaluate(() => document.querySelectorAll('details.dev-section').forEach(d => d.open = true));
  await page.waitForTimeout(1200);
  const tiles = await page.locator('details[data-widget="grid"] .cell-tile').count();
  check('D: the cells section is a grid of the cells alone', tiles === 16, String(tiles));
  const tileVal = await page.locator('details[data-widget="grid"] .cell-tile[title="cell_5"] .table-value').innerText();
  check('D: tiles read at the declared resolution', /^\d\.\d{3}$/.test(tileVal.trim()), tileVal);
  check('D: pack summaries stay rows under the grid',
    await page.locator('details[data-widget="grid"] tr[data-address]', { hasText: 'Average Cell Voltage' }).count() === 1);
  const avgVal = await page.locator('details[data-widget="grid"] tr[data-address]', { hasText: 'Average Cell Voltage' }).locator('.table-value').innerText();
  check('D: a summary in the tiles\' unit reads as finely', /^\d\.\d{3}$/.test(avgVal.trim()), avgVal);
  const cnt = await page.locator('details[data-widget="grid"] tr[data-address]', { hasText: 'Balancing Count' }).locator('.table-value').innerText();
  check('D: other rows keep their own format', /^\d+$/.test(cnt.trim()), cnt);
  const minTile = await page.locator('details[data-widget="grid"] .cell-tile.ct-min[title="cell_5"]').count();
  check('D: the weak cell is marked', minTile === 1);
  const bal = await page.locator('details.dev-section tr', { hasText: 'Balancing Mask' }).innerText().catch(() => '');
  check('D: a mask reads as what is set', /Cell 3/.test(bal), bal.replace(/\s+/g, ' '));
  // the active bit (cell high voltage) and the two derived counts — nothing else
  const shown = await page.locator('details[data-widget="active_only"] tr[data-address]:visible').allInnerTexts();
  const noneShown = await page.locator('details[data-widget="active_only"] .ds-none:visible').count();
  check('D: alarms show only what is active', shown.length === 3 && shown.some(x => /Cell High V/.test(x))
    && noneShown === 0, shown.join(' / ').replace(/\s+/g, ' '));
  const u2 = (g.units || []).find(u => u.unit_id === 2) || {};
  await page.evaluate(id => window.app.openFleetDevice(id), u2.device_id);
  await page.waitForTimeout(2500);
  await page.evaluate(() => document.querySelectorAll('details.dev-section').forEach(d => d.open = true));
  await page.waitForTimeout(1200);
  check('D: a clean pack says nothing is active',
    await page.locator('details[data-widget="active_only"] .ds-none:visible').count() === 1
    && await page.locator('details[data-widget="active_only"] tr[data-address]:visible').count() === 0);

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
