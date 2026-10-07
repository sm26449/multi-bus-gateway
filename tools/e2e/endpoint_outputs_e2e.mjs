/* End-to-end validation of the endpoint's operator-defined totals, InfluxDB
 * tags and extra InfluxDB outputs (3.85.0): everything is set from the
 * endpoint page, persisted, kept by the settings dialog and live in the totals.
 *
 * Needs an EPHEMERAL instance with auth off and a fake pack bus on a PTY with
 * TWO units (a total of one unit is not published):
 *
 *   docker cp tools/e2e/tap_feeder.py <ctr>:/tmp/
 *   docker exec -d -u <app uid> -e TAP_UNITS=2,3 <ctr> python /tmp/tap_feeder.py
 *   cd tools/e2e && MBG_URL=http://127.0.0.1:8099 node endpoint_outputs_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const PORT = process.env.TAP_PORT || '/tmp/ttyTAPE2E';
const ID = 'bank-e2e';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const ep = async () => (await api(`/api/endpoints/${ID}`)).body;
const sleep = ms => new Promise(r => setTimeout(r, ms));

await api(`/api/endpoints/${ID}`, { method: 'DELETE' });
const created = await api('/api/endpoints', { method: 'POST', body: JSON.stringify({
  id: ID, name: 'Bank E2E', template: 'seplos_bms_v3_rtu_tap', enabled: true,
  connection: { protocol: 'rtu_tap', serial_port: PORT, baudrate: 9600 },
  units: [2, 3], influxdb: { enabled: true, bucket: 'e2e' }, mqtt: { topic_prefix: 'e2e/bank/${unit_id}' },
}) });
check('endpoint created', created.status === 200, JSON.stringify(created.body).slice(0, 200));

let p = {};
for (let i = 0; i < 20; i++) {
  p = await ep();
  if ((p.aggregates || {}).pack_total_voltage !== undefined) break;
  await sleep(1000);
}
check('totals computed from the bus', (p.aggregates || {}).pack_total_voltage !== undefined,
  JSON.stringify(p.aggregates || {}).slice(0, 160));
check('template totals exposed', !!(p.totals_declared || {}).pack_max_temp);
check('unit fields exposed', (p.unit_fields || []).includes('average_cell_temp'));

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
// the malformed output in step 4 is a deliberate 422 the browser logs
page.on('console', m => { if (m.type() === 'error' && !/status of 422/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const saveModal = async () => {
  await page.click('#endpointModal [data-endpoint-save]');
  await page.waitForSelector('#endpointModal.active', { state: 'detached' }).catch(() => {});
  await page.waitForFunction(() => !document.querySelector('#endpointModal.active'));
};

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(id => window.app.openEndpointDetail(id), ID);
  await page.waitForSelector('#plTotals table');

  // ---- 1. the template's totals are in the open ---------------------------
  const row = page.locator('#plTotals tr', { hasText: 'pack_max_temp' });
  check('pack_max_temp row shown, from the template', await row.count() === 1
    && /template/i.test(await row.innerText()));

  // ---- 2. override a total from the page -----------------------------------
  await row.locator('[data-action="openTotalModal"]').click();
  await page.waitForSelector('#totFrom');
  await page.check('#totFrom input[value="average_cell_temp"]');
  await page.fill('#totFilter', 'avg');
  check('field filter narrows the list',
    !(await page.isVisible('#totFrom [data-f="pack_voltage"]')));
  await saveModal();
  p = await ep();
  check('override saved', JSON.stringify(p.totals.pack_max_temp.from.sort())
    === JSON.stringify(['average_cell_temp', 'max_cell_temp']), JSON.stringify(p.totals));
  check('row now says yours', /yours/i.test(await page.locator('#plTotals tr', { hasText: 'pack_max_temp' }).innerText()));

  // a new total of the operator's own
  await page.click('#plTotals [data-action="openTotalModal"]:has-text("Add")');
  await page.fill('#totName', 'my_cycles_sum');
  await page.selectOption('#totOp', 'sum');
  await page.check('#totFrom input[value="cycles"]');
  await saveModal();
  let agg = {};
  for (let i = 0; i < 15; i++) { agg = (await ep()).aggregates || {}; if (agg.my_cycles_sum !== undefined) break; await sleep(1000); }
  check('own total computed live (42+42)', agg.my_cycles_sum === 84, String(agg.my_cycles_sum));

  // ---- 3. tags on every unit's points --------------------------------------
  await page.click('#plInfluxExtras [data-action="openInfluxTagsModal"]');
  await page.fill('#plTagRows [data-kv-row] [data-k="k"]', 'battery_id');
  await page.fill('#plTagRows [data-kv-row] [data-k="v"]', '${unit_id}');
  await saveModal();
  p = await ep();
  check('tags saved', (p.influxdb.tags || {}).battery_id === '${unit_id}');
  const dev = (await api('/api/devices')).body.devices.find(d => d.id === `${ID}-u3`);
  check('unit sees its substituted tag', dev !== undefined);
  check('tags shown on the card', /battery_id=\$\{unit_id\}/.test(await page.innerText('#plInfluxExtras')));

  // ---- 4. an extra InfluxDB output -----------------------------------------
  await page.click('#plInfluxExtras [data-action="openInfluxOutputModal"]:has-text("Add")');
  await page.fill('#outId', 'legacy');
  await page.fill('#outMeas', 'e2e_pack');
  const fr = '#outFieldRows [data-kv-row]';
  await page.fill(`${fr} [data-k="name"]`, 'total_voltage');
  await page.fill(`${fr} [data-k="source"]`, 'pack_total_voltage');
  await page.click('#outFieldRows [data-kv-add]');
  await page.locator(`${fr} [data-k="name"]`).nth(1).fill('voltage_kv');
  await page.locator(`${fr} [data-k="source"]`).nth(1).fill('pack_total_voltage');
  await page.locator(`${fr} [data-k="scale"]`).nth(1).fill('0.001');
  await saveModal();
  p = await ep();
  const o = (p.influxdb.outputs || [])[0] || {};
  check('output saved', o.id === 'legacy' && o.measurement === 'e2e_pack' && o.fields.length === 2
    && o.fields[1].scale === 0.001, JSON.stringify(o));
  check('tags untouched by the output save', (p.influxdb.tags || {}).battery_id === '${unit_id}');
  check('output listed', /e2e_pack/.test(await page.innerText('#plInfluxExtras')));

  // a malformed output is refused in the dialog, not saved
  await page.click('#plInfluxExtras [data-action="openInfluxOutputModal"]:has-text("Add")');
  await page.fill('#outId', 'bad');
  await page.fill('#outMeas', '_time');
  await page.fill('#outFieldRows [data-kv-row] [data-k="name"]', 'x');
  await page.click('#endpointModal [data-endpoint-save]');
  await page.waitForFunction(() => document.getElementById('endpointFeedback')?.textContent.trim());
  check('bad output refused with the field named',
    /measurement/.test(await page.textContent('#endpointFeedback')));
  await page.evaluate(() => window.app.closeModal('endpointModal'));
  check('bad output not stored', ((await ep()).influxdb.outputs || []).length === 1);

  // ---- 5. the settings dialog keeps all three ------------------------------
  await page.click('[data-endpoint-page] [data-action="openEndpointModal"]');
  await page.waitForSelector('#plName');
  await page.fill('#plName', 'Bank E2E renamed');
  await page.click('#endpointModal [data-endpoint-save]');
  await page.waitForFunction(() => !document.querySelector('#endpointModal.active'));
  await sleep(500);
  p = await ep();
  check('settings save keeps totals, tags, outputs',
    p.name === 'Bank E2E renamed' && !!p.totals.pack_max_temp && !!p.totals.my_cycles_sum
    && (p.influxdb.tags || {}).battery_id && (p.influxdb.outputs || []).length === 1,
    JSON.stringify({ n: p.name, t: Object.keys(p.totals || {}), tags: p.influxdb.tags, o: (p.influxdb.outputs || []).length }));

  // ---- 6. undo from the page -----------------------------------------------
  await page.evaluate(id => window.app.openEndpointDetail(id), ID);
  await page.waitForSelector('#plTotals table');
  page.once('dialog', d => d.accept());
  await page.click('#plInfluxExtras [data-action="removeInfluxOutput"]');
  await sleep(800);
  await page.click('#plTotals tr:has-text("pack_max_temp") [data-action="removeEndpointTotal"]');
  await sleep(800);
  p = await ep();
  check('output removed', (p.influxdb.outputs || []).length === 0);
  check('override reset to the template', !p.totals.pack_max_temp && !!p.totals.my_cycles_sum);

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
