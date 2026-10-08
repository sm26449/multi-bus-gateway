// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Bridges, phase 3, as a user meets it: finding bridges on the LAN, listening
// to a bus another master polls THROUGH a bridge, and the per-bus tools (the
// monitor filtered to one bus, the probe asking any unit on it).
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node bridges_p3_e2e.mjs
//
// Sims (sims/bridge_sims.py) run inside the gateway container: a transparent
// converter on 4196 and a Modbus TCP gateway on 8899 (ports the bridge kinds
// declare for LAN discovery), and a bus with a foreign master on 14296.
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const APP_UID = process.env.APP_UID || '10001';
const HERE = path.dirname(fileURLToPath(import.meta.url));

const TPL = 'e2e_p3_tpl';
const BR = { t: 'e2e-p3-t', m: 'e2e-p3-m' };
const DEV = { tap1: 'e2e-p3-tap1', tap2: 'e2e-p3-tap2', u1: 'e2e-p3-u1', u2: 'e2e-p3-u2', bad: 'e2e-p3-bad' };
const P = { t: 4196, g: 8899, m: 14296 };

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

function startSim(kind, port) {
  try { inCtr(`rm -f /tmp/br_sim_${port}.stop /tmp/br_sim_${port}.json`); } catch { /* none */ }
  docker(['exec', '-d', '-u', APP_UID, CTR, 'sh', '-c',
          `python3 /tmp/bridge_sims.py ${kind} ${port} > /tmp/br_${port}.log 2>&1`]);
}
const stopSim = port => { try { inCtr(`touch /tmp/br_sim_${port}.stop`); } catch { /* gone */ } };
const simStats = port => { try { return JSON.parse(inCtr(`cat /tmp/br_sim_${port}.json`)); } catch { return {}; } };

async function live(dev) {
  const v = (await api(`/api/values?device=${dev}`)).body?.values || {};
  return Object.fromEntries(Object.values(v).map(x => [x.name, x.value]));
}
async function waitValues(dev, want, secs = 20) {
  let v = {};
  for (let i = 0; i < secs * 2; i++) {
    v = await live(dev);
    if (v.reg_a === want[0] && v.reg_b === want[1]) return v;
    await sleep(500);
  }
  return v;
}
async function cleanup() {
  for (const id of Object.values(DEV)) await del(`/api/devices/${id}`);
  for (const id of Object.values(BR)) await del(`/api/bridges/${id}`);
  await del(`/api/device-templates/${TPL}`);
  try { inCtr('rm -rf /app/config/devices/e2e-p3-*'); } catch { /* none */ }
}

for (const p of Object.values(P)) stopSim(p);
await cleanup();
await sleep(1200);
inCtr('rm -f /tmp/br_sims.stop');          // left by bridges_e2e.mjs: it stops every sim
docker(['cp', path.join(HERE, 'sims', 'bridge_sims.py'), `${CTR}:/tmp/bridge_sims.py`]);
startSim('transparent', P.t);
startSim('gateway', P.g);
startSim('master', P.m);
await sleep(2000);
const tplSave = await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E p3 slave', vendor: 'E2E',
  protocol: { byte_order: 'big', transports: ['tcp', 'rtu'] },
  poll_groups: { normal: { interval: 1 } },
  registers: [
    { address: 0, name: 'reg_a', label: 'Reg A', data_type: 'uint16', poll_group: 'normal' },
    { address: 1, name: 'reg_b', label: 'Reg B', data_type: 'uint16', poll_group: 'normal' },
  ] } });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const card = id => page.locator(`.bridge-card[data-bridge="${id}"]`);
const busRow = (id, port) => card(id).locator(`.bridge-port:has(code:text-is(":${port}"))`);
async function goDevices() {
  await page.click('[data-page="devices"]');
  if (!(await page.isVisible('#devicesListView')))
    await page.click('#deviceRegistersView [data-action="closeDeviceRegisters"]');
  await page.waitForSelector('#devicesListView', { state: 'visible' });
}

try {
  check('template saved (API)', tplSave.status === 200);
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ═══ 1. Find bridges on the LAN, add one from what was found ════════════
  await goDevices();
  await page.locator('button[data-action="openBridgeModal"]:visible').first().click();
  await page.waitForSelector('#bridgeModal.active');
  await page.click('#bridgeModal [data-action="openBridgesDiscover"]');
  await page.waitForSelector('#bridgeInfoModal.active #brFindCidr');
  await page.fill('#brFindCidr', '127.0.0.1/32');
  await page.click('#brFindBtn');
  await page.waitForSelector('#brFindResult table', { timeout: 30000 });
  const found = await page.innerText('#brFindResult');
  check('Find on the LAN: the transparent converter on :4196 is named as transparent',
    /127\.0\.0\.1:4196\s+RS-485 to Ethernet converter — transparent/.test(found), found.replace(/\s+/g, ' '));
  check('…the gateway on :8899 is named as a Modbus TCP gateway',
    /127\.0\.0\.1:8899\s+[^\n]*gateway/i.test(found), found.replace(/\s+/g, ' '));
  check('…the bus with a foreign master (port not declared by any kind) is not listed', !/14296/.test(found));
  await page.locator('#brFindResult tr:has-text("127.0.0.1:4196") [data-action="addFoundBridge"]').click();
  await page.waitForSelector('#bridgeModal.active');
  check('"Add bridge" from a find prefills the kind, the host and the bus',
    await page.isChecked('input[name="brType"][value="rtu_transparent"]')
    && await page.inputValue('#brHost') === '127.0.0.1'
    && await page.locator('#bridgeBody .br-port').first().inputValue() === '4196');
  await page.fill('#brName', 'E2E found converter');
  await page.fill('#brId', BR.t);
  await page.click('#bridgeModal [data-action="saveBridge"]');
  await page.waitForSelector('#bridgeModal', { state: 'hidden', timeout: 20000 });
  await card(BR.t).waitFor();
  check('…and it is saved', (await api(`/api/bridges/${BR.t}`)).status === 200);
  // once added, a second search does not knock on it
  const again = await post('/api/bridges/discover', { cidr: '127.0.0.1/32' });
  check('a host already added is listed as such and not probed again',
    (again.body?.found || []).some(x => x.bridge === BR.t && x.detail === 'already added')
    && !(again.body?.found || []).some(x => x.port === P.t),
    JSON.stringify(again.body).slice(0, 300));

  // ═══ 2. Listen-only through a bridge, from the wizard ═══════════════════
  check('bridge for the bus with a foreign master (API)',
    (await post('/api/bridges', { id: BR.m, name: 'E2E master bus', type: 'rtu_transparent', host: '127.0.0.1',
                                  ports: [{ port: P.m, label: 'BMS bus' }] })).status === 200);
  await goDevices();
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  await page.click('#deviceWizardModal label:has(input[name="devWizRtuMode"][value="rtu_tap"])');
  await page.waitForSelector('#devWizTapWhere');
  const opts = await page.locator('#devWizTapWhere option').allInnerTexts();
  check('Listen only offers "where the bus is": this host, or a transparent bridge\'s bus',
    /serial port on this host/i.test(opts[0]) && opts.some(o => o.includes(`:${P.m}`)), opts.join(' | '));
  await page.selectOption('#devWizTapWhere', `${BR.m}::${P.m}`);
  await page.waitForSelector('#devWizSerial', { state: 'hidden' });
  check('…choosing a bridge hides the serial port and line settings (they are the bus\'s)',
    !(await page.isVisible('#devWizBaud')) && await page.isVisible('#devWizUnitR'));
  await page.fill('#devWizUnitR', '1');
  await page.click('#devWizNext');
  await page.click(`.tpl-pick[data-tpl="${TPL}"]`);
  await page.click('#devWizNext');
  await page.fill('#devWizId', DEV.tap1);
  await page.fill('#devWizName', 'E2E tap 1');
  if (!(await page.isChecked('#devWizEnabled'))) await page.click('label:has(#devWizEnabled)');
  await page.click('#devWizNext');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });
  let v = await waitValues(DEV.tap1, [1111, 2222]);
  check('the tap hears unit 1 through the bridge: 1111 / 2222', v.reg_a === 1111 && v.reg_b === 2222, JSON.stringify(v));
  const dev = (await api('/api/devices')).body.devices.find(d => d.id === DEV.tap1) || {};
  check('…it is a tap on that bridge and bus', dev.protocol === 'rtu_tap' && dev.bridge === BR.m
    && +dev.bridge_port === P.m, JSON.stringify(dev).slice(0, 200));
  check('a second tap on the same bus (unit 2, API)',
    (await post('/api/devices', { id: DEV.tap2, template: TPL, enabled: true,
      connection: { protocol: 'rtu_tap', bridge: BR.m, bridge_port: P.m, unit_id: 2 } })).status === 200);
  v = await waitValues(DEV.tap2, [3333, 4444]);
  check('…hears 3333 / 4444', v.reg_a === 3333 && v.reg_b === 4444, JSON.stringify(v));
  await sleep(1500);
  const ms = simStats(P.m);
  check('the bridge received not one byte from the gateway (listen-only)', ms.written === 0 && ms.accepts >= 1,
    JSON.stringify(ms));
  check('one connection to the bridge for both taps', ms.accepts === 1, JSON.stringify(ms));
  const test = (await post(`/api/devices/${DEV.tap1}/test`, {})).body || {};
  check('Test on the tap reports what it hears, on the bridge\'s bus',
    test.ok === true && test.message.includes(`127.0.0.1:${P.m}`), JSON.stringify(test));

  // a tapped bus: nothing may transmit there
  const polled = await post('/api/devices', { id: DEV.bad, template: TPL, enabled: false,
    connection: { bridge: BR.m, bridge_port: P.m, unit_id: 5 } });
  check('a polled device on a tapped bus is refused, saying why',
    polled.status === 422 && /polled or tapped/.test(JSON.stringify(polled.body)), JSON.stringify(polled.body));
  check('Scan on a tapped bus is refused (409)', (await post(`/api/bridges/${BR.m}/ports/${P.m}/scan`, {})).status === 409);
  check('Check mode on a tapped bus is refused (409)',
    (await post(`/api/bridges/${BR.m}/probe`, { port: P.m, unit_id: 1 })).status === 409);
  await goDevices();
  await card(BR.m).waitFor();
  await page.waitForFunction(id => /listening · [1-9]\d* frames/i.test(
    document.querySelector(`.bridge-card[data-bridge="${id}"]`)?.innerText || ''), BR.m, { timeout: 15000 });
  const mRow = await busRow(BR.m, P.m).innerText();
  check('the bus shows "listening · N frames · 0 CRC errors"', /listening · \d+ frames · 0 CRC errors/i.test(mRow), mRow);
  check('…and offers no Scan, + Device or Check mode there',
    await busRow(BR.m, P.m).locator('[data-action="busScan"], [data-action="bridgeProbe"], [data-action="addDeviceOnBus"]').count() === 0);
  check('…its devices carry the listen-only mark', await busRow(BR.m, P.m).locator('.bi-ear').count() >= 3);

  // ═══ 3. Per-bus diagnostics: the monitor on one bus, the probe any unit ═
  check('polled device on the found converter, unit 1 (API)',
    (await post('/api/devices', { id: DEV.u1, template: TPL, enabled: true,
      connection: { bridge: BR.t, bridge_port: P.t, unit_id: 1 } })).status === 200);
  check('…and unit 2 on the same bus (API)',
    (await post('/api/devices', { id: DEV.u2, template: TPL, enabled: true,
      connection: { bridge: BR.t, bridge_port: P.t, unit_id: 2 } })).status === 200);
  v = await waitValues(DEV.u2, [3333, 4444]);
  check('both read through the one connection', v.reg_a === 3333, JSON.stringify(v));
  await post('/api/bus-trace/config', { enabled: true, clear: true });
  await goDevices();
  await busRow(BR.t, P.t).locator('[data-action="openBusMonitor"]').click();
  await page.waitForSelector('#diagBusFilter', { state: 'visible' });
  await page.waitForFunction(() => document.getElementById('diagBusFilter')?.value === '127.0.0.1:4196');
  check('"Monitor" on a bus opens Diagnostics filtered to that bus',
    await page.inputValue('#diagBusFilter') === `127.0.0.1:${P.t}`);
  const label = await page.locator(`#diagBusFilter option[value="127.0.0.1:${P.t}"]`).innerText();
  check('…the bus is named after its bridge', label.includes('E2E found converter'), label);
  await page.waitForSelector('#diagTableBody tr[data-seq]', { timeout: 15000 });
  await sleep(2500);
  const devsShown = await page.$$eval('#diagTableBody tr[data-seq] td:nth-child(2)', tds => [...new Set(tds.map(t => t.textContent.trim()))]);
  check('the monitor names each unit that asked, on one shared connection',
    devsShown.includes(DEV.u1) && devsShown.includes(DEV.u2), devsShown.join(','));
  check('…and shows only that bus', devsShown.every(d => d.startsWith('e2e-p3-u')), devsShown.join(','));
  const tr = (await api(`/api/bus-trace?bus=127.0.0.1:${P.t}&limit=50`)).body.entries || [];
  check('API: the trace filters by bus', tr.length > 0 && tr.every(e => e.bus === `127.0.0.1:${P.t}`));
  // the probe: unit 2 asked through device 1's connection
  const before = simStats(P.t);
  await page.selectOption('#probeDevice', DEV.u1);
  await page.fill('#probeUnit', '2');
  await page.fill('#probeAddr', '0');
  await page.click('#probeBtn');
  await page.waitForFunction(() => /0D05|No response|exception/i.test(document.getElementById('probeResult')?.innerText || ''), null, { timeout: 15000 });
  const pr = await page.innerText('#probeResult');
  check('the probe reads unit 2 (3333 = 0D05) through unit 1\'s device', /0D05/.test(pr), pr.slice(0, 200));
  await sleep(1200);
  const rows = await page.$$eval('#diagTableBody tr[data-seq] td:nth-child(2)', tds => tds.map(t => t.textContent.trim()));
  check('…and its frames show in the monitor as that unit', rows.some(r => r === `${DEV.u1} → unit 2`), [...new Set(rows)].join(','));
  const ts = simStats(P.t);
  check('…without a second connection to the converter (none opened during the probe)',
    ts.accepts === before.accepts && ts.kicked === before.kicked, `${JSON.stringify(before)} → ${JSON.stringify(ts)}`);
  await post('/api/bus-trace/config', { enabled: false, clear: true });

  // ═══ 4. The bridge drops: the tap says so, then hears again ═════════════
  stopSim(P.m);
  let lost = '';
  for (let i = 0; i < 30 && !lost; i++) {
    const b = (await api(`/api/bridges/${BR.m}`)).body;
    lost = b?.ports?.[0]?.wire?.error || '';
    if (!lost) await sleep(500);
  }
  check('when the bridge goes away, the bus says the link is lost', !!lost, lost);
  startSim('master', P.m);
  let back = false;
  for (let i = 0; i < 30 && !back; i++) {
    const b = (await api(`/api/bridges/${BR.m}`)).body;
    back = !b?.ports?.[0]?.wire?.error && (b?.ports?.[0]?.wire?.frames || 0) > 0;
    if (!back) await sleep(500);
  }
  check('…and the tap reconnects by itself when it is back', back);

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await cleanup();
  for (const p of Object.values(P)) stopSim(p);
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
