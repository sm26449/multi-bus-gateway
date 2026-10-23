// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// An MQTT device's form: the topic, its variables and the password are shown
// as •••• with an eye; the keepalive is the template's, its own (every N s)
// or off, and is saved; the device list does not show the topic.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node mqtt_device_form_e2e.mjs
import { chromium } from 'playwright';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const DEV = 'e2e-gx';

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
const conn = async () => ((await api('/api/devices')).body.devices || []).find(d => d.id === DEV)?.connection || {};

await api(`/api/devices/${DEV}`, { method: 'DELETE' });
const made = await api('/api/devices', { method: 'POST', body: JSON.stringify({
  id: DEV, name: 'E2E GX', template: 'victron_gx_mqtt', enabled: false,
  connection: { protocol: 'mqtt', broker: '127.0.0.1', port: 1883, topic: 'N/c0ffee000001/#',
                vars: { portal: 'c0ffee000001', vebus: '276' }, password: 'pw-e2e' } }) });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

try {
  check('an MQTT device (API)', made.status === 200, JSON.stringify(made.body).slice(0, 200));
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  const rowText = await page.locator(`[data-action="openDeviceDetail"][data-args*="${DEV}"]`).first()
    .evaluate(el => (el.closest('tr, .device-row, .card') || el).innerText);
  check('the list does not show the topic', !rowText.includes('c0ffee000001'), rowText.slice(0, 160));
  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvMqttTopic');

  const type = id => page.locator(`#${id}`).getAttribute('type');
  check('topic, variables and password are masked', await type('ddvMqttTopic') === 'password'
        && await type('ddvMqttVars') === 'password' && await type('ddvMqttPass') === 'password');
  check('the password never reaches the page', (await page.locator('#ddvMqttPass').inputValue()) === '');
  await page.click('[data-action="toggleSecret"][data-args*="ddvMqttTopic"]');
  check('the eye shows the topic', await type('ddvMqttTopic') === 'text'
        && (await page.locator('#ddvMqttTopic').inputValue()) === 'N/c0ffee000001/#');
  await page.click('[data-action="toggleSecret"][data-args*="ddvMqttTopic"]');
  check('and hides it again', await type('ddvMqttTopic') === 'password');
  check('the variables are there', (await page.locator('#ddvMqttVars').inputValue()).includes('portal=c0ffee000001'));

  const opt = await page.locator('#ddvKaMode option[value="template"]').innerText();
  check("the template's keepalive is the default, with its interval", opt.includes('30 s'), opt);
  check('its own fields are hidden until chosen', !(await page.locator('#ddvKaFields').isVisible()));
  await page.selectOption('#ddvKaMode', 'custom');
  check('choosing "its own" shows them', await page.locator('#ddvKaFields').isVisible());
  await page.fill('#ddvKaEvery', '600');
  await page.click('[data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1200);
  let c = await conn();
  check('its own keepalive is saved (600 s)', c.keepalive?.every_s === 600
        && c.keepalive?.topic === 'R/{portal}/keepalive', JSON.stringify(c.keepalive));
  check('the password is kept, still masked', c.password === '******', c.password);

  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvKaMode');
  await page.selectOption('#ddvKaMode', 'off');
  await page.click('[data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1200);
  c = await conn();
  check('off is saved as false', c.keepalive === false, JSON.stringify(c.keepalive));

  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvKaMode');
  await page.selectOption('#ddvKaMode', 'template');
  await page.click('[data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1200);
  c = await conn();
  check("back to the template's: no keepalive of its own", c.keepalive == null, JSON.stringify(c.keepalive));
  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await api(`/api/devices/${DEV}`, { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
