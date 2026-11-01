/* End-to-end validation of the ENDPOINT page (3.47.0, migration P4): the way in
 * from the devices list, the header census/status, the aggregate grid, the unit
 * table (health, hand-written ids and names), the per-unit probe, the
 * endpoint-totals toggle, a unit rename, and the endpoint-aware unit workspace
 * (HTTP/REST sinks offered and declared endpoint-wide, no orphan PQ tab).
 *
 * Against the `endpoint` fixture (it toggles a setting and renames a unit;
 * start it fresh): auth off, one installation `sunfield` over Modbus TCP at
 * 127.0.0.1:9 inside the container — a port that REFUSES, so every unit stays
 * unreachable and the offline/degraded paths are the ones under test (a
 * blackholed host would outrun the per-unit probe's wait) — with three units,
 * the third `sunfield-east` "East roof", `max_connections: 2` and the units'
 * topic prefix `mbg/devices/${device_id}`:
 *
 *   tools/e2e/fixture_up.sh endpoint 18089
 *   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18089 ENDPOINT=sunfield node tools/e2e/endpoint_page_e2e.mjs
 *   docker rm -f mbg-fx-endpoint
 */
import { chromium } from 'playwright';
import { openDeviceTab, saveDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18089';
const ENDPOINT = process.env.ENDPOINT || 'sunfield';
const API = `${BASE}/api/endpoints/${ENDPOINT}`;

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
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

  // ---- 1. the endpoint row is a way INTO the endpoint, not only into its units ---
  const openBtn = page.locator('.endpoint-row button[data-action="openEndpointDetail"]');
  check('endpoint row has an open button', await openBtn.count() === 1);
  await openBtn.first().click();
  await page.waitForTimeout(1200);

  // ---- 2. header: identity, status, census -------------------------------
  check('endpoint page rendered', await page.locator('[data-endpoint-page]').count() === 1);
  check('status word is offline',
        (await page.locator('#plStatusWord').innerText()).trim() === 'offline');
  check('census reads 0/3', (await page.locator('#plCensus').innerText()).startsWith('0/3'));

  // ---- 3. the group total says why it is empty ---------------------------
  check('no totals yet, and it says why',
        (await page.locator('[data-group-totals]').innerText()).toLowerCase().includes('nothing is fresh'));

  // ---- 3b. what the wire costs, measured — under "How it is read" ----------
  await page.evaluate(() => document.querySelectorAll('details[data-group-details]').forEach(d => d.open = true));
  const busLine = (await page.locator('#plBus').count()) ? await page.locator('#plBus').innerText() : '';
  const busApi = (await (await fetch(API)).json()).bus;
  // since 3.67 (a1024b1) the line is shown once something was measured —
  // never a "nothing measured yet" placeholder above the fold
  check('the access point states what it costs, once measured',
        busApi.samples ? /per read/i.test(busLine) : busLine === '', `${busApi.samples} samples · ${busLine.slice(0, 70)}`);
  check('two configured connections are two real ones',
        busApi.max_connections === 2 && busApi.lanes === 2,
        JSON.stringify(busApi));
  // A refused connection is still a TRANSACTION: it is timed like any other,
  // so "nothing measured" only holds before the first attempt. What must always
  // hold is that a floor is claimed only when there is something to derive it
  // from — p95 per read, times the reads a sweep needs, across its connections.
  check('a floor is claimed only when it can be derived',
        busApi.floor_s === null
          ? true
          : (busApi.tx_p95_s > 0 && busApi.reads_per_sweep > 0),
        JSON.stringify({ p50: busApi.tx_p50_s, p95: busApi.tx_p95_s,
                         reads: busApi.reads_per_sweep, floor: busApi.floor_s }));

  // ---- 4. the unit table -------------------------------------------------
  const rows = page.locator('[data-group-units] tr');
  check('three unit rows', await rows.count() === 3, String(await rows.count()));
  const body = await page.locator('[data-group-units]').innerText();
  check('hand-written unit id and name are shown',
        body.includes(`${ENDPOINT}-east`) && body.includes('East roof'));
  check('unit health is not a green ok',
        /degraded|down/.test(body) && !/\bok\b/.test(body), body.slice(0, 60));

  // ---- 5. the output destinations are stated, and live under mbg/ --------
  const pageText = await page.locator('[data-endpoint-page]').innerText();
  check('the totals namespace is mbg/endpoints/<id>', pageText.includes(`mbg/endpoints/${ENDPOINT}/`));

  // ---- 6. per-unit probe -------------------------------------------------
  await page.click('[data-endpoint-page] button[data-action="testEndpointUi"]');
  await page.waitForTimeout(9000);
  const test = await page.locator('#plTestOut').innerText();
  check('test reports every unit',
        (test.match(new RegExp(`${ENDPOINT}-`, 'g')) || []).length === 3, test.slice(0, 60));

  // ---- 7. the endpoint-totals toggle persists -------------------------------
  await page.uncheck('#plAggEnabled');
  await page.waitForTimeout(4000);
  check('aggregates toggle persisted',
        (await (await fetch(API)).json()).aggregates_enabled === false);
  await page.check('#plAggEnabled');
  await page.waitForTimeout(4000);

  // ---- 8. renaming a unit keeps the endpoint's routing ----------------------
  // (L3.10) the unit row's ⋯ is the unit's own menu: Rename… opens the Edit
  // device modal at its Identity (L3.11b)
  await page.click(`[aria-controls="devMenu-installation-${ENDPOINT}-u1"]`);
  await page.click(`#devMenu-installation-${ENDPOINT}-u1 [data-ddv-act="rename"]`);
  await page.waitForSelector('#devEditModal.active #ddvName');
  await page.fill('#ddvName', 'North string');
  await saveDeviceEditor(page);
  await page.waitForTimeout(2000);
  const after = await (await fetch(API)).json();
  check('unit rename persisted',
        after.units.find(u => u.device_id === `${ENDPOINT}-u1`).name === 'North string');
  check('routing survived the rename',
        after.mqtt.topic_prefix === 'mbg/devices/${device_id}');

  // ---- 9. a unit opens into the endpoint-aware device workspace -------------
  await page.evaluate(ep => { location.hash = `#/installations/${ep}`; }, ENDPOINT);
  await page.waitForSelector(`[data-group-units] tr[data-unit="${ENDPOINT}-u2"]`);
  await page.click(`[data-group-units] tr[data-unit="${ENDPOINT}-u2"] button[data-action="openDeviceDetail"]`);
  await page.waitForTimeout(1500);
  check('unit workspace opened',
        (await page.locator('#deviceDetailView .section-header h2').innerText())
          .includes(`${ENDPOINT}-u2`));
  await openDeviceTab(page, 'outputs');                 // the Edit device modal at Outputs (L3.11b)
  await page.waitForTimeout(600);
  const outs = await page.locator('#devEditPanel-outputs').innerText();
  check('HTTP output card is offered on an endpoint unit', outs.includes('HTTP / JSON output'));
  check('REST push card is offered on an endpoint unit', outs.includes('REST push'));
  let wide = 0;
  for (const key of ['http', 'rest']) {                 // each output's panel says its scope
    await page.click(`#outTab-${key}`); await page.waitForTimeout(400);
    wide += /Declared on the (endpoint|installation)/.test(await page.locator('#outDetail').innerText()) ? 1 : 0;
  }
  check('and both say they are endpoint-wide', wide === 2);
  check('no orphan Power Quality tab',
        await page.locator('#deviceDetailView [data-dtab="pq"]').count() === 0);

  check('no console errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) {
  check('script completed', false, String(e).slice(0, 300));
} finally {
  await browser.close();
}

const bad = results.filter(r => !r.ok);
console.log(`\n${results.length - bad.length}/${results.length} checks passed`);
process.exit(bad.length ? 1 : 0);
