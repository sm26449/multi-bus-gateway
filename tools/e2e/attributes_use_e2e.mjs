// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Asset attributes in use, through the UI: a calculated field built on attr()
// previews and saves; sun_elevation() previews; a wrong place or category is
// named; a rule on is_day() and attr() previews; the function palette lists
// them. Needs umg512 in the sandbox. What else it needs it makes when absent
// and removes after: a plant location (Paris), umg512's grid connection
// (45 kW export) and a DISABLED `e2e-gx` (broker on TEST-NET) with
// ess_setpoint enabled — never a command on a device that reaches equipment.
// E2E_TARGET=<device>/<command> names another target instead.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node attributes_use_e2e.mjs
import { chromium } from 'playwright';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const DEV = 'umg512';
const [TDEV, TCMD] = (process.env.E2E_TARGET || 'e2e-gx/ess_setpoint').split('/');
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

// what the checks stand on, made here when absent and undone in `finally`
const made = [];
if (!(await api('/api/attributes')).body?.site?.location) {
  await api('/api/attributes/site/location', { method: 'PUT', body: JSON.stringify({ latitude_deg: 48.86, longitude_deg: 2.35 }) });
  made.push(['/api/attributes/site/location']);
}
if (!(await api(`/api/attributes/devices/${DEV}`)).body?.attributes?.grid_connection) {
  await api(`/api/attributes/devices/${DEV}/grid_connection`, { method: 'PUT', body: JSON.stringify({ role: 'connection_point', max_export_kw: 45 }) });
  made.push([`/api/attributes/devices/${DEV}/grid_connection`]);
}
if (!process.env.E2E_TARGET && !(await api('/api/devices')).body.devices.some(d => d.id === 'e2e-gx')) {
  await api('/api/devices', { method: 'POST', body: JSON.stringify({ id: 'e2e-gx', name: 'E2E GX', template: 'victron_gx_mqtt', enabled: false,
    connection: { protocol: 'mqtt', broker: '192.0.2.9', port: 1883, topic: 'N/e2e/#' } }) });
  await api('/api/devices/e2e-gx/commands/ess_setpoint/enabled', { method: 'PUT', body: JSON.stringify({ enabled: true }) });
  made.push(['/api/devices/e2e-gx']);
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
const fns = (await api('/api/calculated/functions')).body;
const names = JSON.stringify(fns);
check('the palette offers the sun and attr()', ['sun_elevation', 'sun_azimuth', 'is_day', 'attr'].every(n => names.includes(`"${n}"`)));
let savedCalc = false;
try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // a calculated field on the grid meter, built on its own attribute
  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await openDeviceTab(page, 'calculated');
  await page.click('[data-action="calcAdd"]');
  await page.fill('#calcName', 'export_room_e2e');
  await page.fill('#calcExpr', 'attr("umg512", "grid_connection", "max_export_kw") * 1000');
  await page.locator('#calcExpr').dispatchEvent('input');
  await page.waitForFunction(() => /45/.test(document.getElementById('calcPreview')?.textContent || ''));
  check('attr() previews in the calculated editor', (await page.locator('#calcPreview').innerText()).includes('45'));
  await page.fill('#calcExpr', 'sun_elevation()');
  await page.locator('#calcExpr').dispatchEvent('input');
  await page.waitForFunction(() => /^= /.test(document.getElementById('calcPreview')?.textContent || ''));
  check('sun_elevation() previews', /^= -?\d/.test(await page.locator('#calcPreview').innerText()));
  await page.fill('#calcExpr', 'sun_elevation("nope")');
  await page.locator('#calcExpr').dispatchEvent('input');
  await page.waitForFunction(() => /nope/.test(document.getElementById('calcPreview')?.textContent || ''));
  check('a wrong place is named', (await page.locator('#calcPreview').innerText()).includes('no installation or device'));
  await page.fill('#calcExpr', 'attr("umg512", "grid_connection", "max_export_kw") * 1000');
  await page.locator('#calcExpr').dispatchEvent('input');
  await page.click('[data-action="calcSave"]');
  await page.waitForTimeout(1500);
  const sel = (await api(`/api/registers/selected?device=${DEV}`)).body || {};
  savedCalc = JSON.stringify(sel).includes('export_room_e2e');
  check('the calculated field saves', savedCalc);

  // a rule on the sun and an attribute
  await page.click('[data-page="rules"]');
  await page.waitForSelector('#rulesAddBtn');
  await page.click('#rulesAddBtn');
  await page.waitForSelector('#ruleModal.active #rlKind');
  await page.fill('#rlId', 'e2e-sun-attr');
  await page.selectOption('#rlKind', 'condition');
  const opts = await page.locator('#rlTarget option').evaluateAll(os => os.map(o => o.value));
  const tgt = opts.find(v => v.includes(TDEV));
  check('the test target is offered', !!tgt, opts.join(','));
  if (tgt) {
    await page.selectOption('#rlTarget', tgt);
    await page.locator('#rlTarget').dispatchEvent('change');
    if (await page.locator('#rlCommand option').count()) await page.selectOption('#rlCommand', TCMD).catch(() => {});
  }
  await page.fill('#rlWhen', 'is_day(5, "site") and attr("umg512", "grid_connection", "max_export_kw") > 40');
  await page.fill('#rlThen', 'value=-1000');
  await page.fill('#rlElse', 'value=0');
  await page.click('[data-action="previewRule"]');
  await page.waitForTimeout(1200);
  const pv = await page.locator('#rlPreview').innerText();
  check('the rule previews on the sun and the attribute', /false|true|fals|adev/i.test(pv) && !/error|eroare/i.test(pv), pv);
  await page.fill('#rlWhen', 'is_day(5, "nope")');
  await page.click('[data-action="previewRule"]');
  await page.waitForTimeout(1200);
  const pv2 = await page.locator('#rlPreview').innerText();
  check('a wrong place in a rule is named', pv2.includes('nope'), pv2);
  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  for (const [path] of made.reverse()) await api(path, { method: 'DELETE' });
  if (savedCalc) {
    // remove the test field through the same API the editor uses
    const sel = (await api(`/api/registers/selected?device=${DEV}`)).body || {};
    const calc = (sel.calculated || []).filter(c => c.name !== 'export_room_e2e');
    await api(`/api/devices/${DEV}/calculated`, { method: 'PUT', body: JSON.stringify({ calculated: calc }) });
  }
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
