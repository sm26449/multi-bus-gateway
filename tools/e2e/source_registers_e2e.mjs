/* Ticking fields PER SOURCE.
 *
 * A unit reached two ways has two maps: the SunSpec view reads holding
 * registers, the Solar API view reads JSON paths. Editing them as one list
 * would make neither editable, and saving one over the other would leave a
 * source polling addresses that mean nothing to it. This drives the picker and
 * then checks the save landed in the right file and nowhere else.
 *
 *   MBG_URL=http://localhost:18088 DEVICE=pv-u1 CHROMIUM_PATH=<chrome> \
 *     node source_registers_e2e.mjs
 *
 * Config it expects: auth off, one endpoint whose group declares TWO sources
 * (solar_api over HTTP, sunspec over Modbus) on units 1 and 2.
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18088';
const DEV = process.env.DEVICE || 'pv-u1';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const sel = async (src) =>
  (await (await fetch(`${BASE}/api/registers/selected?device=${DEV}&source=${src}`)).json());

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const errs = [];
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

try {
  // ---- the two maps are genuinely different -----------------------------
  const api0 = await sel('solar_api'), mb0 = await sel('sunspec');
  check('each source has its own map',
        api0.registers.length > 0 && mb0.registers.length > api0.registers.length,
        `solar_api ${api0.registers.length}, sunspec ${mb0.registers.length}`);
  check('the API offers the sources so the UI needs no extra call',
        (api0.sources || []).map(s => s.id).join(',') === 'solar_api,sunspec');

  // ---- the picker appears, and only when there is a choice --------------
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(1200);
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
  await page.waitForTimeout(1500);
  await page.locator(`[data-group-units] tr[data-unit="${DEV}"] button[data-action="openDeviceDetail"]`).first().click();
  await page.waitForTimeout(1500);
  await page.locator('#deviceWsTabs .config-main-tab[data-dtab="measurements"]').click();
  await page.waitForTimeout(2500);

  const bar = page.locator('#regSourceBar');
  check('the source picker is shown', (await bar.innerText()).length > 0,
        (await bar.innerText()).slice(0, 60));
  check('it offers both sources',
        await bar.locator('button:has-text("solar_api")').count() === 1
        && await bar.locator('button:has-text("sunspec")').count() === 1);
  check('it says what ticking here means',
        /only what THIS source reads/i.test(await bar.innerText()));

  // ---- switching source switches the map --------------------------------
  // the Available catalog is the SOURCE's map — a SunSpec register map and a
  // JSON-path map have nothing in common, so the picker must change with it
  await page.locator('#deviceRegTabs .config-main-tab[data-regtab="selected"]').click();
  await page.waitForTimeout(800);
  const listed = async () =>
    (await page.locator('#selectedRegistersList').innerText()).trim();
  await bar.locator('button:has-text("sunspec")').click();
  await page.waitForTimeout(3000);
  const sun = await listed();
  await bar.locator('button:has-text("solar_api")').click();
  await page.waitForTimeout(3000);
  const api = await listed();
  check('the selected list actually redraws on a source switch',
        sun !== api && !/No measurements selected/i.test(sun),
        `sunspec ${sun.length} chars, solar_api ${api.length} chars`);
  check('each list shows only that source\'s fields',
        sun.includes('event_flags_1') && !api.includes('event_flags_1'),
        `solar_api shows: ${api.split('\n')[0]}`);

  // ---- a save lands in ONE source's file --------------------------------
  const before = { api: (await sel('solar_api')).registers.length,
                   mb: (await sel('sunspec')).registers.length };
  const keep = (await sel('solar_api')).registers.slice(0, 3);
  const put = await fetch(`${BASE}/api/registers/selected?device=${DEV}&source=solar_api`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(keep.map(r => ({
      address: r.address, name: r.name, label: r.label || r.name,
      unit: r.unit || '', data_type: r.data_type || 'float',
      poll_group: r.poll_group || 'realtime',
      ...(r.json_path ? { json_path: r.json_path } : {}),
    }))),
  });
  check('a per-source save is accepted', put.ok, String(put.status));
  await page.waitForTimeout(1500);
  const after = { api: (await sel('solar_api')).registers.length,
                  mb: (await sel('sunspec')).registers.length };
  check('it changed the source it addressed', after.api === 3,
        `${before.api} -> ${after.api}`);
  check('and left the other source untouched', after.mb === before.mb,
        `${before.mb} -> ${after.mb}`);

  check('no console errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) {
  check('script completed', false, e.message);
} finally {
  const ok = results.filter(r => r.ok).length;
  console.log(`\n${ok}/${results.length} checks passed`);
  await browser.close();
  process.exit(ok === results.length ? 0 : 1);
}
