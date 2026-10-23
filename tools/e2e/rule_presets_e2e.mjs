// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// A template's rule examples, from the UI: the rule editor offers them for a
// device made from the template ("Start from"), fills the form with
// {device} replaced — a condition with a release (hysteresis) included —
// and the rule is saved in shadow.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node rule_presets_e2e.mjs
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const APP_UID = process.env.APP_UID || '10001';
const TPL = 'e2e_rp_tpl';
const DEV = 'e2e-rp-dev';
const RULE = `${DEV}-buffer`;

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
const put = (p, body) => api(p, { method: 'PUT', body: JSON.stringify(body) });
const del = p => api(p, { method: 'DELETE' });
const inCtr = cmd => execFileSync('docker', ['exec', '-u', APP_UID, CTR, 'sh', '-c', cmd], { encoding: 'utf8' });
async function cleanup() {
  await del(`/api/rules/${RULE}`);
  await del(`/api/devices/${DEV}`);
  await del(`/api/device-templates/${TPL}`);
  try { inCtr(`rm -rf /app/config/devices/${DEV}`); } catch { /* none */ }
}

await cleanup();
const made = await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E rule presets', vendor: 'E2E', protocol: { transports: ['tcp'] },
  poll_groups: { normal: { interval: 5 } },
  registers: [
    { address: 0, name: 'water_in', label: 'Water in', unit: '°C', data_type: 'int16', scale: 10 },
    { address: 1, name: 'onoff', label: 'On/off', data_type: 'uint16', writable: true, write_allowed: [0, 1] },
  ],
  commands: { power: { label: 'On / off', params: { value: { allowed: [0, 1], required: true } },
                       writes: [{ register: 'onoff', value: '${value}' }] } },
  rules: [{ id: 'buffer', label: 'On under 25, off over 35', description: 'A buffer thermostat with hysteresis.',
            kind: 'condition', target: { device: '{device}', command: 'power' },
            when: '{device}.water_in < 25', release: '{device}.water_in > 35',
            then: { params: { value: 1 } }, else: { params: { value: 0 } },
            timing: { every_s: 10, debounce: 3, min_interval_s: 900 }, stale_after_s: 120 }],
} });
const dev = await post('/api/devices', { id: DEV, name: 'E2E rule presets', enabled: false, template: TPL,
  connection: { protocol: 'tcp', host: '127.0.0.1', port: 15598, unit_id: 1 } });
const en = await put(`/api/devices/${DEV}/commands/power/enabled`, { enabled: true });

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
try {
  check('a template with a rule example, a device, its command enabled (API)',
    made.status === 200 && dev.status === 200 && en.status === 200, JSON.stringify([made.body, dev.body, en.body]).slice(0, 300));
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="rules"]');
  await page.click('#rulesAddBtn');
  await page.waitForSelector('#ruleModal.active #rlPreset');
  const opt = page.locator('#rlPreset option', { hasText: 'On under 25, off over 35' });
  check('the editor offers the device\'s example', await opt.count() === 1);
  await page.selectOption('#rlPreset', { value: await opt.getAttribute('value') });
  await page.waitForFunction(() => (document.getElementById('rlWhen')?.value || '').includes('water_in'));
  check('picking it fills the form for this device',
    await page.inputValue('#rlId') === RULE && await page.inputValue('#rlWhen') === `${DEV}.water_in < 25`
    && await page.inputValue('#rlUntil') === `${DEV}.water_in > 35` && await page.inputValue('#rlKind') === 'condition'
    && await page.inputValue('#rlTarget') === `d:${DEV}`, JSON.stringify([await page.inputValue('#rlId'), await page.inputValue('#rlUntil'), await page.inputValue('#rlKind'), await page.inputValue('#rlTarget')]));
  check('its description is shown', /hysteresis/.test(await page.textContent('#ruleModal')));
  await page.click('#ruleModal [data-action="saveRule"]');
  await page.waitForSelector('#ruleModal', { state: 'hidden' }).catch(() => {});
  const saved = (await api(`/api/rules/${RULE}`)).body || {};
  const raw = saved.raw || saved;
  check('saved in shadow, with its release', raw.mode === 'shadow' && raw.release === `${DEV}.water_in > 35`
    && raw.when === `${DEV}.water_in < 25`, JSON.stringify({mode: raw.mode, release: raw.release, keys: Object.keys(raw)}));
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
