// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Commands over MQTT in the UI: an MQTT template shows a write-topic column,
// a row's payload in its details, availability and keepalive in the
// protocol block — all saved; a command result shows "publish … → topic"
// and "already set".
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node mqtt_commands_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const TPL = 'e2e_mqtt_cmd';

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

await api(`/api/device-templates/${TPL}`, { method: 'DELETE' });
const made = await api('/api/device-templates', { method: 'POST', body: JSON.stringify({ device_template: {
  id: TPL, name: 'E2E MQTT commands', vendor: 'E2E', protocol: { transports: ['mqtt'] },
  registers: [
    { address: 1, name: 'setpoint', unit: 'W', data_type: 'float', category: 'settings',
      topic: 'N/{portal}/settings/sp', json_path: 'value', writable: true },
    { address: 2, name: 'soc', unit: '%', data_type: 'float', category: 'battery',
      topic: 'N/{portal}/soc', json_path: 'value' },
  ] } }) });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const row = i => `#devTplBody tr[data-idx="${i}"]`;
const saved = async () => {
  const b = (await api(`/api/device-templates/${TPL}`)).body;
  return b?.device_template || b?.template || b || {};
};

try {
  check('an MQTT template (API)', made.status === 200, JSON.stringify(made.body).slice(0, 200));
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="templates"]');
  await page.click(`[data-tm-edit="${TPL}"]`);
  await page.waitForSelector('#devTplModal.active');

  check('a write-topic column on an MQTT template', await page.locator('#devTplBody th:has-text("write topic")').count() === 1);
  const wt = page.locator(`${row(0)} input[data-f="write_topic"]`);
  await wt.fill('W/{portal}/settings/sp');
  await wt.dispatchEvent('change');

  check('availability and keepalive in the protocol block', await page.locator('#tplKaTopic').count() === 1
        && await page.locator('#tplAvTopic').count() === 1);
  await page.fill('#tplKaTopic', 'R/{portal}/keepalive');
  await page.fill('#tplKaFirst', '');
  await page.fill('#tplKaPayload', '{"keepalive-options": ["suppress-republish"]}');
  await page.fill('#tplKaEvery', '30');

  await page.click(`${row(0)} [data-action="tplRowExtras"]`);
  await page.waitForSelector('#tplRowModal.active');
  check('the row details offer the MQTT payload', await page.locator('#tplRowModal :text("Written over MQTT")').count() >= 1);
  const pl = page.locator('#tplRowModal input[aria-label="write payload"]');
  await pl.fill('{"value": ${value}}');
  await pl.dispatchEvent('change');
  await page.click('#tplRowModal [data-action="tplRowDone"] >> nth=-1');

  await page.click('#devTplModal [data-action="saveDevTemplate"], #devTplModal button:has-text("Save")');
  await page.waitForTimeout(800);
  const t = await saved();
  const r0 = (t.registers || [])[0] || {};
  check('the write topic and payload are saved', r0.write_topic === 'W/{portal}/settings/sp'
        && r0.write_payload === '{"value": ${value}}', JSON.stringify(r0));
  const ka = (t.protocol || {}).keepalive || {};
  check('the keepalive is saved', ka.topic === 'R/{portal}/keepalive' && ka.first_payload === ''
        && ka.every_s === 30 && ka.payload.includes('suppress-republish'), JSON.stringify(t.protocol));
  check('no availability when its topic is left empty', !(t.protocol || {}).availability, JSON.stringify(t.protocol));

  // a command result as the device page shows it
  const html = await page.evaluate(() => window.app._cmdResultHtml({
    status: 'dry_run', device: 'gx', ms: 3,
    frames: [{ topic: 'W/x/settings/sp', payload: '{"value": -5000}', registers: ['setpoint'], values: {} }] }));
  check('a dry run shows what it would publish', html.includes('publish') && html.includes('W/x/settings/sp')
        && html.includes('{&quot;value&quot;: -5000}'), html);
  const already = await page.evaluate(() => window.app._cmdResultHtml({ status: 'success', already_set: true, frames: [], ms: 1 }));
  check('"already set" is said', already.includes('already set'), already);
  const modbus = await page.evaluate(() => window.app._cmdResultHtml({
    status: 'dry_run', frames: [{ address: 40232, words: [9500], registers: ['p'], values: {} }] }));
  check('a Modbus frame still reads as before', modbus.includes('40232'), modbus);
  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await api(`/api/device-templates/${TPL}`, { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
