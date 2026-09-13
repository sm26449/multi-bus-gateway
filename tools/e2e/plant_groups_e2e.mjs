/* The plant page end to end: groups, sources, and every button that changes
 * them. A PV plant holds inverters AND the meter at its grid connection, each
 * group with its own units, sources and total — this walks an operator's whole
 * path through that, because a model is only real if it can be driven.
 *
 *   docker run -d --name mbg-plant -p 127.0.0.1:18085:18085 -e UI_PORT=18085 \
 *     --entrypoint python -v "$PWD":/app:ro -v /tmp/plantcfg:/app/config \
 *     -w /app multi-bus-gateway:test main.py
 *
 *   MBG_URL=http://localhost:18085 ENDPOINT=sunfield \
 *     CHROMIUM_PATH=<chrome> node plant_groups_e2e.mjs
 *
 * Config it expects: auth off, one endpoint with an `inverters` group (units
 * 1,2) and a `grid` group (unit 240, id <endpoint>-meter-240), pointed at a
 * host that REFUSES so the offline paths are the ones under test.
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18085';
const EP = process.env.ENDPOINT || 'sunfield';
const API = `${BASE}/api/endpoints/${EP}`;
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const api = async () => (await fetch(API)).json();

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
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
  await page.waitForTimeout(1500);
  // "How it is read" is folded by default; the operator opens it — and it stays
  // open across the page's live tick
  const openHow = () => page.evaluate(() =>
    document.querySelectorAll('details[data-group-details]').forEach(d => d.open = true));
  await openHow();

  // ---- 1. the plant shows a card per group ------------------------------
  const cards = page.locator('[data-group]');
  check('a card per group', await cards.count() === 2, `${await cards.count()}`);
  check('the inverter group is there', await page.locator('[data-group="inverters"]').count() === 1);
  check('the meter group is there', await page.locator('[data-group="grid"]').count() === 1);

  // ---- 2. each card states what it is and where it publishes ------------
  const inv = await page.locator('[data-group="inverters"]').innerText();
  const grid = await page.locator('[data-group="grid"]').innerText();
  check('the group states its role', /inverter/i.test(inv) && /meter/i.test(grid));
  check('the first group owns the headline topic',
        inv.includes(`mbg/endpoints/${EP}/`) && !inv.includes(`mbg/endpoints/${EP}/grid`));
  // a group of ONE unit is that unit — it promises no total and no topic
  check('a group of one unit promises no total',
        !grid.includes(`mbg/endpoints/${EP}/grid/`)
        && (await api()).groups.find(g => g.id === 'grid').topic === null);
  check('the meter unit kept its hand-written id', grid.includes(`${EP}-meter-240`));
  check('the inverter group lists its units',
        inv.includes(`${EP}-u1`) && inv.includes(`${EP}-u2`));
  check('each group shows its sources',
        await page.locator('[data-group="inverters"] [data-src-row]').count() > 0
        && await page.locator('[data-group="grid"] [data-src-row]').count() > 0);

  // ---- 3. a group can be switched off on its own ------------------------
  await page.locator('[data-group="grid"] input[type=checkbox]').first().uncheck();
  await page.waitForTimeout(2500);
  let d = await api();
  const gridG = d.groups.find(g => g.id === 'grid');
  check('disabling a group persists', gridG.enabled === false);
  check('its units stay visible', gridG.units.length === 1);
  check('the other group is untouched',
        d.groups.find(g => g.id === 'inverters').enabled === true);
  await page.waitForTimeout(1500);
  await page.locator('[data-group="grid"] input[type=checkbox]').first().check();
  await page.waitForTimeout(2500);
  check('re-enabling works too', (await api()).groups.find(g => g.id === 'grid').enabled === true);

  // ---- 4. adding a source to one group only -----------------------------
  await openHow();
  await page.locator('[data-group="inverters"] button[data-action="openSourceModal"]').first().click();
  await page.waitForTimeout(900);
  await page.fill('#srcId', 'solar_api');
  await page.selectOption('#srcProto', 'http');
  await page.waitForTimeout(300);
  await page.fill('#srcUrl', 'http://127.0.0.1:9/api.cgi?DeviceId=${unit_id}');
  await page.selectOption('#srcTpl', 'fronius_solar_api_inverter');
  await page.fill('#srcIvRealtime', '5');
  await page.fill('#srcStale', '30');
  await page.click('#endpointModal [data-endpoint-save]');
  await page.waitForTimeout(3000);
  d = await api();
  const invSrc = d.groups.find(g => g.id === 'inverters').sources.map(s => s.id);
  check('the source landed in its group', invSrc.includes('solar_api'), invSrc.join(','));
  check('the other group was not touched',
        !d.groups.find(g => g.id === 'grid').sources.map(s => s.id).includes('solar_api'));
  const added = d.groups.find(g => g.id === 'inverters').sources.find(s => s.id === 'solar_api');
  check('the URL resolved per unit is stored once, with the placeholder',
        (added.address || '').includes('${unit_id}'), added.address);

  // ---- 5. order is precedence, and the arrows change it ----------------
  await page.waitForTimeout(1000);
  const before = (await api()).groups.find(g => g.id === 'inverters').sources.map(s => s.id);
  await openHow();
  await page.locator('[data-group="inverters"] [data-src-row="solar_api"] button[data-action="moveSource"]').first().click();
  await page.waitForTimeout(3000);
  const after = (await api()).groups.find(g => g.id === 'inverters').sources.map(s => s.id);
  // relative, not absolute: the arrow raises a source by ONE place, whatever
  // else the group already holds
  check('raising precedence moves the source up one place',
        after.indexOf('solar_api') === before.indexOf('solar_api') - 1,
        `${before} -> ${after}`);

  // ---- 6. a source can be removed, the last one cannot -----------------
  page.once('dialog', dlg => dlg.accept());
  await openHow();
  await page.locator('[data-group="inverters"] [data-src-row="solar_api"] button[data-action="deleteSource"]').first().click();
  await page.waitForTimeout(3000);
  check('a source can be removed',
        !(await api()).groups.find(g => g.id === 'inverters').sources.map(s => s.id).includes('solar_api'));
  // the grid group has exactly one source; whatever it is called, removing it
  // must be refused — a unit needs a way to be read
  await openHow();
  const lastDel = page.locator('[data-group="grid"] [data-src-row] button[data-action="deleteSource"]').first();
  check('the last source cannot be removed', await lastDel.isDisabled());

  // ---- 7. adding a whole group -----------------------------------------
  await page.locator('button[data-action="openGroupModal"]').first().click();
  await page.waitForTimeout(900);
  await page.fill('#grpId', 'battery');
  await page.selectOption('#grpRole', 'battery');
  await page.selectOption('#grpTpl', 'eastron_sdm120');
  await page.fill('#grpUnits', '30, 31');
  await page.fill('#grpAddr', '127.0.0.1');
  await page.fill('#grpPort', '9');
  await page.click('#endpointModal [data-endpoint-save]');
  await page.waitForTimeout(3500);
  d = await api();
  const bat = d.groups.find(g => g.id === 'battery');
  check('a new group is created', !!bat);
  check('with its units', bat && bat.units.map(u => u.unit_id).join(',') === '30,31');
  check('and its own topic', bat && bat.topic === `mbg/endpoints/${EP}/battery`);

  // ---- 8. a unit id cannot be claimed twice ----------------------------
  const bad = await fetch(API, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: EP, name: d.name, enabled: true, connection: d.connection,
      units: [1, 2],
      groups: [
        { id: 'inverters', template: 'fronius_sunspec_inverter', units: [1, 2],
          connection: { protocol: 'tcp', host: '127.0.0.1', port: 9 } },
        { id: 'clash', template: 'eastron_sdm120', units: [2],
          connection: { protocol: 'tcp', host: '127.0.0.1', port: 9 } },
      ],
    }),
  });
  check('two groups cannot claim one unit id', bad.status === 422, String(bad.status));

  // ---- 9. removing a group ---------------------------------------------
  page.once('dialog', dlg => dlg.accept());
  await page.locator('[data-group="battery"] button[data-action="deleteGroup"]').first().click();
  await page.waitForTimeout(3500);
  check('a group can be removed',
        !(await api()).groups.some(g => g.id === 'battery'));

  check('no console errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) {
  check('script completed', false, e.message);
} finally {
  const ok = results.filter(r => r.ok).length;
  console.log(`\n${ok}/${results.length} checks passed`);
  await browser.close();
  process.exit(ok === results.length ? 0 : 1);
}
