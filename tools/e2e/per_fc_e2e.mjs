// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// One address, four registers (3.91): coil 0, discrete 0, holding 0 and
// input 0 are different things on a real device. A user makes a map holding
// all four in the visual editor, reads a device, and sees four values; every
// tool keeps them apart (selection, edit, the live store, the monitor).
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node per_fc_e2e.mjs
//
// Device: the Modbus TCP gateway sim (sims/bridge_sims.py) on 15502, unit 1:
// holding 0 = 5555, input 0 = 9001, coil 0 = 1, discrete 0 = 0.
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const APP_UID = process.env.APP_UID || '10001';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const TPL = 'e2e_fc_tpl';
const DEV = 'e2e-fc-dev';
const PORT = 15502;
const WANT = { h0: 5555, i0: 9001, c0: 1, d0: 0 };
const KEY = { h0: 0, i0: 100000, c0: 200000, d0: 300000 };

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
const sleep = ms => new Promise(r => setTimeout(r, ms));
const docker = args => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
const inCtr = cmd => docker(['exec', '-u', APP_UID, CTR, 'sh', '-c', cmd]);
async function cleanup() {
  await del(`/api/devices/${DEV}`);
  await del(`/api/device-templates/${TPL}`);
  try { inCtr(`rm -rf /app/config/devices/${DEV}`); } catch { /* none */ }
}

await cleanup();
try { inCtr(`rm -f /tmp/br_sims.stop /tmp/br_sim_${PORT}.stop`); } catch { /* none */ }
docker(['cp', path.join(HERE, 'sims', 'bridge_sims.py'), `${CTR}:/tmp/bridge_sims.py`]);
docker(['exec', '-d', '-u', APP_UID, CTR, 'sh', '-c', `python3 /tmp/bridge_sims.py gateway ${PORT} > /tmp/br_${PORT}.log 2>&1`]);
await sleep(2500);

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
async function setCell(loc, v) { await loc.fill(String(v)); await loc.dispatchEvent('change'); }

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ═══ 1. the visual editor takes four rows at address 0 ══════════════════
  await page.click('[data-page="templates"]');
  await page.waitForSelector('#tmNewBtn', { state: 'visible' });
  await page.click('#tmNewBtn');
  await page.waitForSelector('#devTplModal.active');
  await page.fill('#tplId', TPL);
  await page.fill('#tplName', 'E2E four tables');
  const rows = [
    { address: 0, name: 'h0', data_type: 'uint16', fc: 'holding' },
    { address: 0, name: 'i0', data_type: 'uint16', fc: 'input' },
    { address: 0, name: 'c0', data_type: 'uint16', fc: 'coil' },
    { address: 0, name: 'd0', data_type: 'uint16', fc: 'discrete' },
  ];
  for (let i = 0; i < rows.length; i++) {
    if (i) await page.click('#devTplBody [data-action="tplAddRow"]');
    const row = page.locator('#devTplBody tbody tr').nth(i);
    await setCell(row.locator('[data-f="address"]'), rows[i].address);
    await setCell(row.locator('[data-f="name"]'), rows[i].name);
    await setCell(row.locator('[data-f="label"]'), rows[i].name.toUpperCase());
    await setCell(row.locator('[data-f="data_type"]'), rows[i].data_type);
    await row.locator('select[data-f="register_type"]').selectOption(rows[i].fc);
  }
  await page.click('#devTplSaveBtn');
  await page.waitForSelector('#devTplModal', { state: 'hidden' }).catch(() => {});
  const tplErr = await page.locator('#tplErrors').textContent().catch(() => '');
  const t = (await api(`/api/device-templates/${TPL}`)).body?.device_template || (await api(`/api/device-templates/${TPL}`)).body || {};
  check('the editor saves coil 0, discrete 0, holding 0 and input 0 in one map',
    (t.registers || []).length === 4, `${tplErr} ${JSON.stringify(t).slice(0, 200)}`);
  if (await page.isVisible('#devTplModal')) await page.evaluate(() => window.app.closeModal('devTplModal'));

  // a repeat within ONE table is still refused, with words
  const dup = await post('/api/device-templates', { device_template: { id: 'e2e_fc_dup', name: 'dup', vendor: 'E2E',
    protocol: { transports: ['tcp'] }, registers: [
      { address: 0, name: 'a', data_type: 'uint16', register_type: 'coil' },
      { address: 0, name: 'b', data_type: 'uint16', register_type: 'coil' }] } });
  check('two coils at 0 are refused, naming the table',
    dup.status === 422 && /duplicate address 0 in the coil table/.test(JSON.stringify(dup.body)), JSON.stringify(dup.body).slice(0, 200));

  // ═══ 2. a device reads all four ═════════════════════════════════════════
  check('device on the sim (API)', (await post('/api/devices', { id: DEV, template: TPL, enabled: true,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: PORT, unit_id: 1 } })).status === 200);
  let vals = {};
  for (let i = 0; i < 40; i++) {
    vals = (await api(`/api/values?device=${DEV}`)).body?.values || {};
    if (Object.keys(vals).length >= 4) break;
    await sleep(500);
  }
  const byName = Object.fromEntries(Object.entries(vals).map(([k, v]) => [v.name, { key: +k, value: v.value, address: v.address, rt: v.register_type }]));
  for (const n of Object.keys(WANT))
    check(`${n}: ${WANT[n]} under key ${KEY[n]} (address 0)`,
      byName[n]?.value === WANT[n] && byName[n]?.key === KEY[n] && byName[n]?.address === 0, JSON.stringify(byName[n]));

  // ═══ 3. the Measurements page keeps them apart ══════════════════════════
  const sel = (await api(`/api/registers/selected?device=${DEV}`)).body;
  const selRegs = sel?.registers || sel || [];
  check('API: each selected register carries its key',
    selRegs.length === 4 && selRegs.every(r => r.key === KEY[r.name]), JSON.stringify(selRegs.map(r => [r.name, r.key])));
  // the live store survives a re-save of the selection (purge keeps every table)
  await post(`/api/registers/selected?device=${DEV}`, selRegs);
  await sleep(3000);
  vals = (await api(`/api/values?device=${DEV}`)).body?.values || {};
  check('re-saving the selection keeps all four values', Object.keys(vals).length >= 4, Object.keys(vals).join(','));
  // in the UI: the device's Measurements list has four rows; unticking
  // input 0 removes input 0 only
  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await page.click('#deviceDetailView [data-dtab="measurements"]');
  const list = page.locator('#deviceRegistersView');
  await list.locator('#selectedRegistersList tr[data-address="100000"]').waitFor();
  const keysShown = await list.locator('#selectedRegistersList tr[data-address]').evaluateAll(trs => trs.map(t => t.dataset.address).sort());
  check('Measurements lists four rows, one per table', keysShown.join(',') === '0,100000,200000,300000', keysShown.join(','));
  // the row re-renders on change, so click it directly (no post-click state check)
  await list.locator('#selectedRegistersList tr[data-address="100000"] input[type=checkbox]').evaluate(el => el.click());
  await sleep(1500);
  const after = (await api(`/api/registers/selected?device=${DEV}`)).body;
  const left = (after?.registers || after || []).map(r => r.name).sort();
  check('unticking input 0 in the UI removes input 0 only', left.join(',') === 'c0,d0,h0', left.join(','));
  await sleep(3000);
  vals = (await api(`/api/values?device=${DEV}`)).body?.values || {};
  const names = Object.values(vals).map(v => v.name).sort();
  check('dropping input 0 drops only input 0', names.join(',') === 'c0,d0,h0', names.join(','));

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await cleanup();
  try { inCtr(`touch /tmp/br_sim_${PORT}.stop`); } catch { /* gone */ }
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
