// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// A device with no meter names the meter on its supply (consumption) from
// its Meters tab; the meter's power and energy fields are offered by unit,
// the link is saved (still in its earlier form, energy_meter, while it is
// the only link), and the device gains an Energy tab.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node linked_meter_e2e.mjs
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const APP_UID = process.env.APP_UID || '10001';
const HP = 'e2e-lm-hp', MT = 'e2e-lm-meter';

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
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });
const del = p => api(p, { method: 'DELETE' });
const inCtr = cmd => execFileSync('docker', ['exec', '-u', APP_UID, CTR, 'sh', '-c', cmd], { encoding: 'utf8' });
async function cleanup() {
  for (const d of [HP, MT]) { await del(`/api/devices/${d}`); try { inCtr(`rm -rf /app/config/devices/${d}`); } catch { /* none */ } }
}
const row = (a, n, u) => ({ address: a, name: n, label: n, unit: u, data_type: 'float', poll_group: 'normal',
  mqtt: { enabled: true, topic: '' }, influxdb: { enabled: true, measurement: '', tags: {} }, ui: {} });

await cleanup();
const conn = { protocol: 'tcp', host: '127.0.0.1', port: 15597, unit_id: 1 };
const a = await post('/api/devices', { id: HP, name: 'E2E heat pump', enabled: false, connection: conn });
const b = await post('/api/devices', { id: MT, name: 'E2E meter', enabled: false, connection: conn });
inCtr(`mkdir -p /app/config/devices/${MT} && cat > /app/config/devices/${MT}/selected_registers.json <<'J'
${JSON.stringify({ version: '1.0', poll_groups: {}, registers: [row(0, 'power_active_total', 'kW'), row(2, 'energy_active_import', 'kWh'), row(4, 'voltage_l1_n', 'V')] })}
J`);

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
try {
  check('two devices, the meter with power, energy and voltage (API)', a.status === 200 && b.status === 200);
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]'); await page.waitForTimeout(800);
  await page.evaluate(id => window.app.openDeviceDetail(id), HP);
  await page.waitForSelector('#deviceWsTabs');
  check('no energy counter yet → no Energy tab', await page.locator('#deviceWsTabs [data-dtab="energy"]').count() === 0);
  await openDeviceTab(page, 'meters');
  await page.waitForSelector('#mtrRows[data-ready]');
  check('an empty row guesses no role', await page.inputValue('#mtrRole-0') === '');
  await page.selectOption('#mtrDev-0', MT);
  check('a meter picked takes the first free role: consumption', await page.inputValue('#mtrRole-0') === 'consumption');
  await page.waitForFunction(() => document.querySelectorAll('#mtrPower-0 option').length > 1);
  const powerOpts = await page.locator('#mtrPower-0 option').allTextContents();
  check('only power fields are offered for power, preselected', powerOpts.length === 2 && /kW/.test(powerOpts[1])
    && await page.inputValue('#mtrPower-0') === 'power_active_total', powerOpts.join(' | '));
  check('the energy counter is preselected', await page.inputValue('#mtrEnergy-0') === 'energy_active_import');
  await page.click('#mtrSave');
  await page.waitForFunction(() => /✓/.test(document.getElementById('mtrFeedback')?.textContent || ''), null, { timeout: 8000 }).catch(() => {});
  const dev = ((await api('/api/devices')).body.devices || []).find(d => d.id === HP) || {};
  check('saved: the link', dev.energy_meter?.device === MT && dev.energy_meter?.power === 'power_active_total'
    && dev.energy_meter?.energy === 'energy_active_import', JSON.stringify(dev.energy_meter));
  await page.evaluate(id => window.app.openDeviceDetail(id), HP);
  await page.waitForSelector('#deviceWsTabs');
  check('the device now has an Energy tab', await page.locator('#deviceWsTabs [data-dtab="energy"]').count() === 1);
  check('no page errors', errors.length === 0, errors.join(' | '));
} catch (e) {
  check('suite ran to the end', false, e.stack || String(e));
} finally {
  await browser.close();
  await cleanup();
}
const ok = results.filter(r => r.ok).length;
console.log(`\n${ok}/${results.length} passed`);
process.exit(ok === results.length ? 0 : 1);
