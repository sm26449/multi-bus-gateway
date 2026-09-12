/* End-to-end validation of the ENDPOINT page (3.47.0, migration P4): the way in
 * from the devices list, the header census/status, the aggregate grid, the unit
 * table (health, hand-written ids and names), the per-unit probe, the
 * endpoint-totals toggle, a unit rename, and the endpoint-aware unit workspace
 * (HTTP/REST sinks offered and declared endpoint-wide, no orphan PQ tab).
 *
 * Run against an EPHEMERAL instance — it toggles settings and renames a unit:
 *
 *   docker run -d --name mbg-e2e -p 127.0.0.1:18080:18080 -e UI_PORT=18080 \
 *     --entrypoint python -v "$PWD":/app:ro -v /tmp/e2ecfg:/app/config \
 *     -w /app multi-bus-gateway:test main.py
 *
 *   cd tools/e2e && MBG_URL=http://localhost:18080 ENDPOINT=sunfield \
 *     CHROMIUM_PATH=<chrome> node endpoint_page_e2e.mjs
 *
 * The config it expects: auth off, and one endpoint on a TEST-NET host (so every
 * unit stays unreachable and the offline/degraded paths are the ones under
 * test) with three units, the third carrying an explicit id `<endpoint>-east` and
 * the name "East roof". The endpoint declares `max_connections: 2` and an
 * `mqtt.topic_prefix` of `mbg/devices/${device_id}`. Point it at a host that
 * REFUSES rather than blackholes (127.0.0.1:9), or the per-unit probe outruns
 * the script's wait.
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18080';
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

  // ---- 3. the aggregate grid says why it is empty ------------------------
  check('no totals yet, and it says why',
        (await page.locator('#plAggGrid').innerText()).toLowerCase().includes('nothing is fresh'));

  // ---- 3b. what the wire costs, measured ----------------------------------
  const busLine = await page.locator('#plBus').innerText();
  check('the access point states what it costs',
        /no transactions measured yet|per read/i.test(busLine), busLine.slice(0, 70));
  const busApi = (await (await fetch(API)).json()).bus;
  check('two configured connections are two real ones',
        busApi.max_connections === 2 && busApi.lanes === 2,
        JSON.stringify(busApi));
  check('nothing polled, so no floor is claimed',
        busApi.tx_p50_s === null && busApi.floor_s === null);

  // ---- 4. the unit table -------------------------------------------------
  const rows = page.locator('#plUnitsBody tr');
  check('three unit rows', await rows.count() === 3, String(await rows.count()));
  const body = await page.locator('#plUnitsBody').innerText();
  check('hand-written unit id and name are shown',
        body.includes(`${ENDPOINT}-east`) && body.includes('East roof'));
  check('unit health is not a green ok',
        /degraded|down/.test(body) && !/\bok\b/.test(body), body.slice(0, 60));

  // ---- 5. the output destinations are stated, and live under mbg/ --------
  const pageText = await page.locator('[data-endpoint-page]').innerText();
  check('output namespace is mbg/endpoints/<id>', pageText.includes(`mbg/endpoints/${ENDPOINT}/`));

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
  await page.click(`#plUnitsBody tr[data-unit="${ENDPOINT}-u1"] button[data-action="endpointRenameUnit"]`);
  await page.fill('#plUnitName', 'North string');
  await page.click('#plUnitsBody button[data-action="saveEndpointUnitName"]');
  await page.waitForTimeout(5000);
  const after = await (await fetch(API)).json();
  check('unit rename persisted',
        after.units.find(u => u.device_id === `${ENDPOINT}-u1`).name === 'North string');
  check('routing survived the rename',
        after.mqtt.topic_prefix === 'mbg/devices/${device_id}');

  // ---- 9. a unit opens into the endpoint-aware device workspace -------------
  await page.click(`#plUnitsBody tr[data-unit="${ENDPOINT}-u2"] button[data-action="openDeviceDetail"]`);
  await page.waitForTimeout(1500);
  check('unit workspace opened',
        (await page.locator('#deviceDetailView .section-header h2').innerText())
          .includes(`${ENDPOINT}-u2`));
  await page.click('[data-dtab="outputs"]');
  await page.waitForTimeout(600);
  const outs = await page.locator('[data-dpanel="outputs"]').innerText();
  check('HTTP output card is offered on an endpoint unit', outs.includes('HTTP / JSON output'));
  check('REST push card is offered on an endpoint unit', outs.includes('REST push'));
  check('and both say they are endpoint-wide',
        (outs.match(/Declared on the endpoint/g) || []).length === 2);
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
