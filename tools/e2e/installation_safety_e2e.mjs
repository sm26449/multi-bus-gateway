/* Safety of the installation page: the three ways it could lie or destroy.
 *
 *   1. Edit → Save on a grouped installation must keep every group and every
 *      source (it flattened the plant to one Modbus group before).
 *   2. Test units must ask every source, not a Modbus host nobody declared,
 *      and say its verdict in words, not colour alone.
 *   3. A unit with nothing to read is idle — grey and named so — never green.
 *
 *   docker run -d --name mbg-safety --network host --entrypoint python \
 *     -v "$PWD":/app:ro -v /tmp/safetycfg:/app/config -w /app \
 *     multi-bus-gateway:test main.py
 *   MBG_URL=http://localhost:18087 ENDPOINT=sunfield CHROMIUM_PATH=<chrome> \
 *     node installation_safety_e2e.mjs
 *
 * Config it expects: auth off; one grouped endpoint — `inverters` (units 1,2;
 * sources solar_api http + sunspec tcp), `grid` (unit 240), and `spare`
 * (unit 7, no template → nothing selected → idle) — all on hosts that REFUSE.
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18087';
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

  const before = await api();
  check('fixture: three groups', before.groups.length === 3, before.groups.map(g => g.id).join(','));

  // ---- 1. Edit is a settings dialog on a grouped installation -------------
  await page.locator('[data-endpoint-page] button[data-action="openEndpointModal"]').click();
  await page.waitForTimeout(500);
  const modal = page.locator('#endpointModal');
  check('the dialog is titled for an installation',
        /installation|instala/i.test(await modal.locator('#endpointModalTitle').innerText()));
  check('no connection fields on a grouped installation',
        await modal.locator('#plHost, #plUnits, #plTemplate, #plTopic').count() === 0);
  check('the name field has a label', await modal.locator('label[for="plName"]').count() === 1);
  check('every checkbox sits in a label',
        await modal.locator('input[type=checkbox]').count() ===
        await modal.locator('label input[type=checkbox]').count());
  await modal.locator('#plName').fill('Sunfield PV renamed');
  await modal.locator('[data-endpoint-save]').click();
  await page.waitForTimeout(1500);
  const after = await api();
  check('the rename landed', after.name === 'Sunfield PV renamed', after.name);
  check('every group survived the save',
        after.groups.map(g => g.id).join(',') === before.groups.map(g => g.id).join(','),
        after.groups.map(g => g.id).join(','));
  check('every source survived the save',
        JSON.stringify(after.groups[0].sources.map(s => s.id)) === JSON.stringify(['solar_api', 'sunspec']),
        after.groups[0].sources.map(s => s.id).join(','));
  check('the units are still the groups\' units',
        after.units.map(u => u.device_id).join(',') === before.units.map(u => u.device_id).join(','),
        after.units.map(u => u.device_id).join(','));
  check('the page redrew with the new name',
        (await page.locator('[data-endpoint-page] h2').innerText()).includes('renamed'));

  // ---- 2. Test units asks every source ------------------------------------
  await page.locator('[data-endpoint-page] button[data-action="testEndpointUi"]').click();
  await page.waitForSelector('#plTestOut [data-test-source]', { timeout: 30000 });
  const out = page.locator('#plTestOut');
  check('the result is announced as a status region', await out.locator('[role="status"]').count() === 1);
  check('one block per source', await out.locator('[data-test-source]').count() >= 3,
        `${await out.locator('[data-test-source]').count()}`);
  const txt = await out.innerText();
  check('the HTTP source was asked as HTTP', /solar_api[\s\S]*http/.test(txt));
  check('a Modbus source was asked on its own connection', /sunspec[\s\S]*tcp/.test(txt));
  check('the verdict is a word, not a colour', /no answer|answered|not probed/.test(txt));
  check('nothing was "blocked: host required"', !/blocked: host required/.test(txt));
  check('the result sits under the header, above the groups', await page.evaluate(() => {
    const o = document.getElementById('plTestOut'), g = document.getElementById('plGroups');
    return o && g && o.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING;
  }));
  await out.locator('button:has-text("Close")').click();
  check('the result can be dismissed', (await out.innerText()).trim() === '');

  // ---- 3. nothing to read → idle, named so ---------------------------------
  const spare = after.units.find(u => u.group_id === 'spare');
  check('the empty unit reports idle over the API', spare && spare.health === 'idle', spare && spare.health);
  const row = page.locator(`[data-group="spare"] [data-group-units] tr[data-unit="${spare.device_id}"]`);
  check('the empty unit says "idle" in its row', /idle/.test(await row.innerText()));
  check('the empty unit is not counted online',
        /0\/1/.test(await page.locator('[data-group="spare"]').innerText()));
  check('no page errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 300));
} finally {
  // leave the fixture as it was found
  try {
    const p = await api();
    await fetch(API, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: EP, name: 'Sunfield PV', enabled: p.enabled,
                             connection: p.connection, aggregates: p.aggregates_enabled }) });
  } catch (e) { /* best effort */ }
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}
