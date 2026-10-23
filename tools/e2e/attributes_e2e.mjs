// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Asset attributes in the UI: the plant's location (one paste fills both
// fields; shown coarse to anyone who may not see secrets), a meter marked as
// the grid connection with its limits, an invalid value named by the server,
// a delete.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node attributes_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const DEV = process.env.ATTR_DEVICE || 'umg512';
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
const cleanup = async () => {
  await api('/api/attributes/site/location', { method: 'DELETE' });
  await api(`/api/attributes/devices/${DEV}/grid_connection`, { method: 'DELETE' });
};
await cleanup();

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const dlg = await watchDialogs(page);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|5\d\d)/.test(m.text())) errs.push(m.text()); });
try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // the plant: Settings → Plant
  await page.click('[data-page="config"]');
  await page.click('#configSubtabs [data-cfgtab="plant"]');
  await page.waitForSelector('#plantAttrHost .attr-card');
  check('the plant offers the location', (await page.locator('#plantAttrHost .attr-add-sel option[value="location"]').count()) === 1);
  await page.selectOption('#plantAttrHost .attr-add-sel', 'location');
  await page.click('#plantAttrHost [data-action="attrAdd"]');
  const lat = page.locator('#plantAttrHost .attr-editor-host input[data-attr-f="latitude_deg"]');
  check('coordinates are masked', (await lat.getAttribute('type')) === 'password');
  await lat.focus();
  await page.evaluate(() => {
    const el = document.querySelector('#plantAttrHost .attr-editor-host input[data-attr-f="latitude_deg"]');
    const dt = new DataTransfer(); dt.setData('text', '48.85661, 2.35221');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  });
  check('one paste fills both', (await page.locator('#plantAttrHost .attr-editor-host input[data-attr-f="longitude_deg"]').inputValue()) === '2.35221');
  await page.click('#plantAttrHost [data-action="attrSave"]');
  await page.waitForSelector('#plantAttrHost .attr-cats [data-attr-cat="location"]');
  const all = (await api('/api/attributes')).body;
  check('saved; anyone who may not see secrets reads it coarse', all.site.location?.latitude_deg === 48.86, JSON.stringify(all.site));
  check('the publishing setting is on the page', (await page.locator('#attrPublish').count()) === 1);

  // a meter as the grid connection
  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await openDeviceTab(page, 'attributes');
  await page.waitForSelector('#deviceDetailView .attr-card');
  const first = await page.locator('#deviceDetailView .attr-add-sel option').first().getAttribute('value');
  check('a meter is offered the grid connection first', first === 'grid_connection', first);
  check('the device shows where its location comes from', (await page.locator('#deviceDetailView .attr-card').innerText()).includes('site'));
  await page.selectOption('#deviceDetailView .attr-add-sel', 'grid_connection');
  await page.click('#deviceDetailView [data-action="attrAdd"]');
  await page.selectOption('#deviceDetailView .attr-editor-host select[data-attr-f="role"]', 'sub_meter');
  await page.fill('#deviceDetailView .attr-editor-host input[data-attr-f="max_export_kw"]', '8');
  await page.click('#deviceDetailView [data-action="attrSave"]');
  await page.waitForTimeout(500);
  check('the server names what is wrong', (await page.locator('#deviceDetailView .attr-err').innerText()).includes('only a connection point'));
  await page.selectOption('#deviceDetailView .attr-editor-host select[data-attr-f="role"]', 'connection_point');
  await page.fill('#deviceDetailView .attr-editor-host input[data-attr-f="max_import_kw"]', '10');
  await page.click('#deviceDetailView [data-action="attrSave"]');
  await page.waitForSelector('#deviceDetailView .attr-cats [data-attr-cat="grid_connection"]');
  const gc = (await api(`/api/attributes/devices/${DEV}`)).body.attributes.grid_connection;
  check('the grid connection is saved', gc?.max_import_kw === 10 && gc?.max_export_kw === 8, JSON.stringify(gc));
  dlg.answers.push(true);
  await page.click('#deviceDetailView [data-attr-cat="grid_connection"] [data-action="attrDelete"]');
  await page.waitForTimeout(600);
  check('and deleted', !(await api(`/api/attributes/devices/${DEV}`)).body.attributes.grid_connection);
  // back to the plant while the device's section is still in the page
  await page.click('[data-page="config"]');
  await page.click('#configSubtabs [data-cfgtab="plant"]');
  await page.waitForSelector('#plantAttrHost [data-attr-cat="location"]');
  await page.click('#plantAttrHost [data-attr-cat="location"] [data-action="attrEdit"]');
  check('the plant editor opens in the plant section', (await page.locator('#plantAttrHost .attr-editor-host input[data-attr-f="latitude_deg"]').count()) === 1
        && (await page.locator('#deviceDetailView .attr-editor-host input').count()) === 0);
  check('rounded coordinates are not offered for saving', (await page.locator('#plantAttrHost .attr-editor-host input[data-attr-f="latitude_deg"]').inputValue()) === '');
  await page.click('#plantAttrHost [data-action="attrCancel"]');
  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await cleanup();
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
