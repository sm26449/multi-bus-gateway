/* BRIDGES, end to end: the box an RS-485 bus is reached through.
 *
 * Two simulated converters (sims/bridge_sims.py, run inside the container):
 *   - a TRANSPARENT converter (raw RTU over TCP, slaves 1 and 2, ONE client at
 *     a time — newest wins, like a Waveshare in "Protocol: None"), and
 *   - a Modbus TCP GATEWAY (pymodbus, unit ids 1 and 2).
 * The script adds the bridges from the Devices page, two slaves on one bus with
 * the Add Device wizard (exact values from both, ONE shared connection: the
 * converter counts its accepts), the duplicate-unit refusal, the wizard's Test
 * connection through the shared bus while they poll, Check mode (right type and
 * deliberately wrong types), a device behind the gateway, editing a bridge
 * (its devices follow), the MBG serial bridge's Set-up command, delete-in-use
 * refused, and the Devices page at phone width.
 *
 * Needs an EPHEMERAL instance with auth off, as a container this script can
 * `docker exec` into (CTR, default mbg-ui-sandbox; app uid 10001). It creates
 * template e2e_br_tpl, bridges e2e-br-*, devices e2e-br-*, sims on ports
 * 14196-14198 and 15502, and removes them at the start (idempotent) and end.
 *
 *   cd tools/e2e && CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:8099 node bridges_e2e.mjs
 *
 * Product bugs known at the time of writing are checked as `bug()`: the check
 * states the CORRECT behaviour, prints BUG while the product is wrong, and does
 * not fail the run unless STRICT=1 (it prints FIXED once the product is right).
 */
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const NET = process.env.DOCKER_NET || 'pv-stack-network';
const APP_UID = process.env.APP_UID || '10001';
const STRICT = process.env.STRICT === '1';
const HERE = path.dirname(fileURLToPath(import.meta.url));

const TPL = 'e2e_br_tpl';
const BR = { t: 'e2e-br-t', g: 'e2e-br-g', s: 'e2e-br-s', tg: 'e2e-br-tg', gt: 'e2e-br-gt' };
const DEV = { u1: 'e2e-br-u1', u2: 'e2e-br-u2', u3: 'e2e-br-u3', g2: 'e2e-br-g2', g2b: 'e2e-br-g2b' };
const P = { t: 14196, t2: 14197, tSpare: 14198, g: 15502, gSpare: 15503 };
// what the sims hold (sims/bridge_sims.py)
const WANT = { t1: [1111, 2222], t2: [3333, 4444], g2: [7777, 8888] };

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + String(extra).replace(/\s+/g, ' ').slice(0, 260) : ''}`);
};
const bugs = [];
const bug = (name, cond, extra = '') => {
  if (cond) { console.log(`FIXED ${name} (known bug no longer reproduces — make it a check())`); return; }
  bugs.push(name);
  if (STRICT) results.push({ name, ok: false });
  console.log(`BUG  ${name}${extra ? ' — ' + String(extra).replace(/\s+/g, ' ').slice(0, 260) : ''}`);
};
const notes = [];
const note = msg => { notes.push(msg); console.log(`NOTE ${msg}`); };
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const text = await r.text();
  let body; try { body = JSON.parse(text); } catch { body = text; }
  return { status: r.status, body };
};
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });
const put = (p, body) => api(p, { method: 'PUT', body: JSON.stringify(body) });
const del = p => api(p, { method: 'DELETE' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const docker = (args, opts = {}) => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...opts });
const inCtr = (cmd, user = APP_UID) => docker(['exec', '-u', user, CTR, 'sh', '-c', cmd]);
const errText = b => JSON.stringify(b?.detail?.errors || b?.detail || b);

// ── sims ──────────────────────────────────────────────────────────────────
function startSim(kind, port) {
  try { inCtr(`rm -f /tmp/br_sim_${port}.stop /tmp/br_sim_${port}.json`); } catch { /* none */ }
  docker(['exec', '-d', '-u', APP_UID, CTR, 'sh', '-c',
          `python3 /tmp/bridge_sims.py ${kind} ${port} > /tmp/br_${port}.log 2>&1`]);
}
function stopSim(port) { try { inCtr(`touch /tmp/br_sim_${port}.stop`); } catch { /* gone */ } }
function simStats(port) {
  try { return JSON.parse(inCtr(`cat /tmp/br_sim_${port}.json`)); } catch { return { accepts: -1, frames: {} }; }
}
async function freshStats(port) { await sleep(700); return simStats(port); }   // the sim dumps every 0.5 s

/** name → {value, ts} of a device's live store */
async function live(dev) {
  const v = (await api(`/api/values?device=${dev}`)).body.values || {};
  return Object.fromEntries(Object.values(v).map(x => [x.name, { value: x.value, ts: x.ts }]));
}
async function waitValues(dev, want, secs = 20) {
  let v = {};
  for (let i = 0; i < secs * 2; i++) {
    v = await live(dev);
    if (v.reg_a?.value === want[0] && v.reg_b?.value === want[1]) return v;
    await sleep(500);
  }
  return v;
}
const shown = v => `${v.reg_a?.value},${v.reg_b?.value}`;
/** newest sample after `since` (epoch s) on every device? */
async function allFreshSince(devs, since, secs = 10) {
  for (let i = 0; i < secs * 2; i++) {
    const ok = [];
    for (const d of devs) ok.push(((await live(d)).reg_a?.ts || 0) > since);
    if (ok.every(Boolean)) return true;
    await sleep(500);
  }
  return false;
}

async function cleanup() {
  for (const id of Object.values(DEV)) await del(`/api/devices/${id}`);
  for (const id of Object.values(BR)) await del(`/api/bridges/${id}`);
  await del(`/api/device-templates/${TPL}`);
  // a deleted device keeps its measurement file on purpose — start from nothing
  try { inCtr('rm -rf /app/config/devices/e2e-br-*'); } catch { /* none */ }
}
function stopSims() { try { inCtr('touch /tmp/br_sims.stop'); } catch { /* not running */ } }

stopSims();
await cleanup();
await sleep(1200);
inCtr('rm -f /tmp/br_sims.stop');
docker(['cp', path.join(HERE, 'sims', 'bridge_sims.py'), `${CTR}:/tmp/bridge_sims.py`]);
startSim('transparent', P.t);
startSim('gateway', P.g);
// the container's own LAN address: a bridge "moving" from 127.0.0.1 to it is
// the same sim, reached on another host — the sim records the client's address
const CTR_IP = docker(['inspect', '-f', `{{(index .NetworkSettings.Networks "${NET}").IPAddress}}`, CTR]).trim();
await sleep(2000);

// the measurement map: two uint16 holding registers, read every second — made
// over the API (the template editor has its own e2e in new_user_e2e.mjs)
const tplSave = await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E bridge slave', vendor: 'E2E',
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
let promptAnswer = '1';
page.on('dialog', d => d.accept(d.type() === 'prompt' ? promptAnswer : undefined));
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

const card = id => page.locator(`.bridge-card[data-bridge="${id}"]`);
const closeToasts = () => page.evaluate(() => document.querySelectorAll('.toast').forEach(t => t.remove()));
async function lastToast() {
  await page.waitForSelector('.toast');
  return (await page.locator('.toast').last().innerText()).replace(/\s+/g, ' ');
}
async function goDevices() {
  await page.click('[data-page="devices"]');
  if (!(await page.isVisible('#devicesListView')))
    await page.click('#deviceRegistersView [data-action="closeDeviceRegisters"]');
  await page.waitForSelector('#devicesListView', { state: 'visible' });
}
async function addBridge({ type, name, id, host, ports = [] }) {
  await goDevices();
  await page.locator('button[data-action="openBridgeModal"]:visible').first().click();
  await page.waitForSelector('#bridgeModal.active');
  await page.click(`#bridgeModal label.bridge-type:has(input[name="brType"][value="${type}"])`);
  await page.fill('#brName', name);
  await page.fill('#brId', id);
  await page.fill('#brHost', host);
  for (let i = 0; i < ports.length; i++) {
    if (i >= await page.locator('#bridgeBody .br-port').count())
      await page.click('#bridgeBody [data-action="bridgeAddPort"]');
    await page.locator('#bridgeBody .br-port').nth(i).fill(String(ports[i].port));
    await page.locator('#bridgeBody .br-label').nth(i).fill(ports[i].label || '');
    await page.locator('#bridgeBody .br-serial').nth(i).fill(ports[i].serial || '');
  }
  await page.click('#bridgeModal [data-action="saveBridge"]');
  await page.waitForSelector('#bridgeModal', { state: 'hidden', timeout: 20000 });
}
async function wizTest() {
  const out = page.locator('#devWizTestResult2');
  await out.evaluate(el => { el.textContent = ''; });
  await page.locator('#deviceWizardModal [data-action="devWizardTest"]:visible').first().click();
  await page.waitForFunction(() => document.getElementById('devWizTestResult2')?.textContent.trim(), null, { timeout: 20000 });
  return (await out.textContent()).trim();
}
/** steps 2 and 3 of the wizard after the bus is chosen on step 1 */
async function wizFinish(id, name) {
  await page.click('#devWizNext');
  await page.click(`.tpl-pick[data-tpl="${TPL}"]`);
  await page.click('#devWizNext');
  await page.fill('#devWizId', id);
  await page.fill('#devWizName', name);
  if (!(await page.isChecked('#devWizEnabled'))) await page.click('label:has(#devWizEnabled)');
  await page.click('#devWizNext');
}
async function busButton(bridgeId, port, action) {
  await goDevices();
  await card(bridgeId).locator(`.bridge-port:has(code:text-is(":${port}")) [data-action="${action}"]`).click();
}

try {
  check('template for the slaves saved (API)', tplSave.status === 200, JSON.stringify(tplSave.body).slice(0, 120));
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ═══ 1. A transparent converter on the Devices page ═════════════════════
  // Modbus is not under the HTTP SSRF guard: a converter on 127.0.0.1 is fine
  await addBridge({ type: 'rtu_transparent', name: 'E2E transparent', id: BR.t, host: '127.0.0.1',
    ports: [{ port: P.t, label: 'panel A', serial: '9600 8N1' }, { port: P.tSpare, label: 'panel B' }] });
  await card(BR.t).waitFor();
  const tHead = await card(BR.t).innerText();
  check('transparent bridge appears on the Devices page with its host and both buses',
    /127\.0\.0\.1/.test(tHead) && tHead.includes(`:${P.t}`) && tHead.includes(`:${P.tSpare}`) && /panel A/.test(tHead),
    tHead);
  check('…its state is "unknown" until a device reads through it', /unknown|no device/i.test(tHead), tHead);
  let b = (await api(`/api/bridges/${BR.t}`)).body;
  check('API: transparent bridge resolves to rtu-tcp, serial note kept',
    b.protocol === 'rtu-tcp' && b.ports?.[0]?.serial === '9600 8N1', JSON.stringify(b).slice(0, 200));

  // ═══ 2. Two slaves on one bus, through the wizard ═══════════════════════
  // 2a. device 1 from the main Add Device button: Modbus RTU → Over network
  await goDevices();
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  await page.click('#deviceWizardModal label:has(input[name="devWizRtuMode"][value="rtu-tcp"])');
  await page.waitForSelector('#devWizBridge');
  check('wizard (RTU over network) offers the bridge picker', await page.inputValue('#devWizBridge') === BR.t,
    await page.inputValue('#devWizBridge'));
  check('…and hides the legacy adapter scan', !(await page.isVisible('#devWizLegacyScan')));
  // pick the OTHER bus on the preselected bridge: the choice must stick
  await page.selectOption('#devWizBridgePort', String(P.tSpare));
  check('wizard: choosing a bus on the preselected bridge keeps that bus',
    await page.inputValue('#devWizBridgePort') === String(P.tSpare),
    `picked :${P.tSpare}, the picker shows :${await page.inputValue('#devWizBridgePort')} (app-wizard.js _devWizBridgeChanged: d.bridge is '' on a fresh wizard, so the first change of the BUS is taken for a change of BRIDGE and resets bridge_port)`);
  await page.selectOption('#devWizBridgePort', String(P.t));
  check('wizard bus picker on :' + P.t, await page.inputValue('#devWizBridgePort') === String(P.t));
  check('…hint: first device on this bus', /First device/i.test(await page.innerText('#devWizBridgeHint2')));
  await page.fill('#devWizUnitBr', '1');
  const t1 = await wizTest();
  check('wizard Test (unit 1, nobody on the bus yet) succeeds', /^✓/.test(t1), t1);
  await wizFinish(DEV.u1, 'E2E slave 1');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });

  let v = await waitValues(DEV.u1, WANT.t1);
  check('slave 1 reads 1111 / 2222 through the transparent bridge', shown(v) === WANT.t1.join(), shown(v));

  // 2b. device 2 from the bus's own "+ Device" button (prefilled bridge, bus, next free unit)
  await busButton(BR.t, P.t, 'addDeviceOnBus');
  await page.waitForSelector('#deviceWizardModal.active');
  await page.waitForSelector('#devWizUnitBr');
  check('"+ Device" on a bus prefills bridge, bus and the next free unit (2)',
    await page.inputValue('#devWizBridge') === BR.t && await page.inputValue('#devWizBridgePort') === String(P.t)
    && await page.inputValue('#devWizUnitBr') === '2',
    `${await page.inputValue('#devWizBridge')} :${await page.inputValue('#devWizBridgePort')} unit ${await page.inputValue('#devWizUnitBr')}`);
  const hint2 = await page.innerText('#devWizBridgeHint2');
  check('…and says who else is on that bus', /E2E slave 1 \(unit 1\)/.test(hint2), hint2);
  const t2 = await wizTest();
  check('wizard Test (unit 2) goes through the bus slave 1 already holds, and says so',
    /^✓/.test(t2) && /one connection|already reads/i.test(t2), t2);
  await wizFinish(DEV.u2, 'E2E slave 2');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });
  v = await waitValues(DEV.u2, WANT.t2);
  check('slave 2 reads 3333 / 4444 on the same bus', shown(v) === WANT.t2.join(), shown(v));
  v = await waitValues(DEV.u1, WANT.t1, 5);
  check('slave 1 still reads 1111 / 2222', shown(v) === WANT.t1.join(), shown(v));

  // one shared connection: in steady state the converter accepts nobody new
  await sleep(2000);
  let s0 = await freshStats(P.t);
  await sleep(6000);
  let s1 = await freshStats(P.t);
  check('ONE shared connection: no new accept in 6 s while both poll (no eviction storm)',
    s1.accepts === s0.accepts && s1.accepts >= 1, `accepts ${s0.accepts} → ${s1.accepts}, kicked ${s1.kicked}`);
  check('…and both slaves were asked on it', (s1.frames['1'] - s0.frames['1']) >= 3 && (s1.frames['2'] - s0.frames['2']) >= 3,
    JSON.stringify(s1.frames));
  if (s1.accepts > 1)
    note(`adding the 2nd slave reopened the shared socket once (accepts=${s1.accepts}): connect() is a forced reopen for every sibling — harmless with "newest wins", visible as a reconnect on the box`);

  // listed under the bus, not in the plain device list
  await goDevices();
  const tCard = await card(BR.t).innerText();
  check('both devices listed under the bridge bus, with their unit ids',
    /unit 1\s*E2E slave 1/.test(tCard) && /unit 2\s*E2E slave 2/.test(tCard), tCard);
  // the plain list = #devicesList without the bridge cards; it must still hold
  // the other devices (so the check is not vacuous) but not the two slaves
  const plain = await page.evaluate(() => {
    const c = document.getElementById('devicesList').cloneNode(true);
    c.querySelectorAll('.bridge-card').forEach(x => x.remove());
    return { text: c.textContent.replace(/\s+/g, ' '), rows: c.children.length };
  });
  check('…and not in the plain device list', plain.rows > 0 && !/E2E slave [12]/.test(plain.text),
    `${plain.rows} rows: ${plain.text.slice(0, 160)}`);
  check('bridge state is online (its devices read)', /online/i.test((await card(BR.t).locator('.bridge-head').innerText())));
  const devRow = (await api('/api/devices')).body.devices.find(d => d.id === DEV.u1) || {};
  check('API: device names its bridge + bus, host/protocol come from it',
    devRow.bridge === BR.t && +devRow.bridge_port === P.t && devRow.protocol === 'rtu-tcp' && devRow.host === '127.0.0.1',
    JSON.stringify({ b: devRow.bridge, p: devRow.bridge_port, pr: devRow.protocol, h: devRow.host }));

  // 2c. the same unit id twice on a bus is refused, in words
  await busButton(BR.t, P.t, 'addDeviceOnBus');
  await page.waitForSelector('#devWizUnitBr');
  check('"+ Device" now proposes unit 3', await page.inputValue('#devWizUnitBr') === '3');
  await page.fill('#devWizUnitBr', '1');
  await wizFinish(DEV.u3, 'E2E duplicate');
  await page.waitForFunction(() => document.getElementById('devWizFeedback')?.textContent.trim());
  const dupMsg = await page.textContent('#devWizFeedback');
  check('a 3rd device with unit 1 on that bus is refused, saying which device has it',
    /unit 1/.test(dupMsg) && new RegExp(DEV.u1).test(dupMsg) && /own address/.test(dupMsg), dupMsg);
  check('…and nothing was created', !(await api('/api/devices')).body.devices.some(d => d.id === DEV.u3));

  // ═══ 3. Test connection while they poll ═════════════════════════════════
  // still in the wizard: back to step 1, unit 2 (a slave a running device reads)
  await page.click('#devWizBack'); await page.click('#devWizBack');
  await page.fill('#devWizUnitBr', '2');
  s0 = await freshStats(P.t);
  const since = Date.now() / 1000;
  const t3 = await wizTest();
  check('wizard Test of unit 2 while both poll: ✓ through the shared bus',
    /^✓/.test(t3) && /one connection|taking turns/.test(t3), t3);
  s1 = await freshStats(P.t);
  check('the Test opened no connection of its own (the live one was not kicked)',
    s1.accepts === s0.accepts, `accepts ${s0.accepts} → ${s1.accepts}`);
  const t4 = await (async () => { await page.fill('#devWizUnitBr', '9'); return wizTest(); })();
  check('wizard Test of an absent unit (9) on the shared bus: ✗, says the bus itself works',
    /^✗/.test(t4) && /bus itself works/.test(t4), t4);
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));
  const s4 = await freshStats(P.t);
  if (s4.accepts > s1.accepts)
    note(`a Test of an ABSENT unit on a shared bus times out and then REOPENS the shared socket (accepts ${s1.accepts} → ${s4.accepts}): every sibling reconnects and stalls for the test's timeout`);
  check('both devices keep reading after the Tests (fresh samples)', await allFreshSince([DEV.u1, DEV.u2], since + 2));
  if (/on the bus '[^']+' already reads/.test(t3))
    note(`shared-bus Test message reads as if the bus were named after a device: "${t3.slice(0, 120)}"`);

  // ═══ 3b. Phase 2: busy bus, scan, add from the scan, export / import ═════
  await goDevices();
  const busyTxt = await card(BR.t).locator('.bridge-port').first().innerText();
  check('the bus says how busy it is (measured)', /\d+% busy/i.test(busyTxt), busyTxt.replace(/\s+/g, ' ').slice(0, 140));
  // a quick sweep over 1-4 through the API (the UI sweeps 1-247 — about 75 s)
  const sj = (await api(`/api/bridges/${BR.t}/ports/${P.t}/scan`, { method: 'POST',
      body: JSON.stringify({ from: 1, to: 4, timeout: 0.3 }) })).body;
  let sres = {};
  for (let i = 0; i < 40; i++) {
    sres = (await api(`/api/bus-scan/${sj.job}`)).body;
    if (sres.state !== 'running') break;
    await sleep(500);
  }
  const units = (sres.found || []).map(f => f.unit_id);
  check('scanning the bus finds both slaves, and says which device already reads each',
    units.join(',') === '1,2' && (sres.found || []).every(f => f.device), JSON.stringify(sres.found || []).slice(0, 200));
  // the UI: Scan opens with progress; the add button prefills the wizard
  await card(BR.t).locator('.bridge-port').first().locator('button', { hasText: 'Scan' }).click();
  await page.waitForSelector('#bridgeInfoModal.active #busScanState');
  await sleep(2500);
  const prog = await page.textContent('#busScanState');
  check('the Scan panel shows progress', /asked|Done/.test(prog), prog);
  // closing the panel must stop the scan on the server
  const uiJob = await page.evaluate(() => window.app._busScanJob);
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));
  let jst = '';
  for (let i = 0; i < 10; i++) {
    jst = (await api(`/api/bus-scan/${uiJob}`)).body.state;
    if (jst !== 'running' && jst !== 'cancelling') break;
    await sleep(500);
  }
  check('closing the Scan panel stops the scan on the server', jst === 'cancelled', jst);
  await page.evaluate(([b, p]) => window.app.addScannedDevice(b, p, 9, ''), [BR.t, P.t]);
  await page.waitForSelector('#deviceWizardModal.active');
  const pre = await page.evaluate(() => ({ b: window.app._devWiz.data.bridge, u: window.app._devWiz.data.unit_id }));
  check('Add from a scan opens the wizard on that bus and unit', pre.b === BR.t && pre.u === 9, JSON.stringify(pre));
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));
  const exp = await (await fetch(BASE + '/api/bridges/export')).text();
  check('bridges export as YAML', /bridges:/.test(exp) && exp.includes(BR.t));
  const imp = (await api('/api/bridges/import', { method: 'POST', body: JSON.stringify({ yaml: exp }) })).body;
  check('importing the export here says they exist (nothing written)', (imp.bridges || []).every(x => x.status === 'exists'),
    JSON.stringify(imp.bridges || []).slice(0, 160));

  // ═══ 4. Check mode (probe) ══════════════════════════════════════════════
  promptAnswer = '1';
  await busButton(BR.t, P.t, 'bridgeProbe');
  await page.waitForSelector('#bridgeInfoModal.active');
  await page.waitForFunction(() => /RTU/.test(document.getElementById('bridgeInfoBody')?.innerText || '')
    && !/Asking/.test(document.getElementById('bridgeInfoBody')?.innerText || ''), null, { timeout: 20000 });
  const pv = await page.innerText('#bridgeInfoBody');
  check('Check mode on the transparent bus: "speaks the way this bridge is set up", RTU answers',
    /speaks the way this bridge is set up/.test(pv) && /RTU\s+answers/.test(pv), pv);
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));
  const sinceProbe = Date.now() / 1000;
  check('devices recover after the probe took the line for a moment',
    await allFreshSince([DEV.u1, DEV.u2], sinceProbe + 1, 15));

  // wrong types (made over the API: the modal is already exercised above)
  let r = await post('/api/bridges', { id: BR.tg, name: 'transparent declared as gateway', type: 'modbus_gateway',
    host: '127.0.0.1', ports: [{ port: P.t }] });
  check('a gateway-type bridge on the transparent sim saves', r.status === 200, errText(r.body));
  r = await post(`/api/bridges/${BR.tg}/probe`, { port: P.t, unit_id: 1 });
  check('Check mode, declared gateway but transparent: verdict says TRANSPARENT mode',
    r.body.speaks === 'rtu' && /TRANSPARENT mode/.test(r.body.verdict || ''), r.body.verdict);
  r = await post('/api/bridges', { id: BR.gt, name: 'gateway declared as transparent', type: 'rtu_transparent',
    host: '127.0.0.1', ports: [{ port: P.g }] });
  r = await post(`/api/bridges/${BR.gt}/probe`, { port: P.g, unit_id: 1 });
  check('Check mode, declared transparent but a gateway: verdict says GATEWAY mode',
    r.body.speaks === 'modbus_tcp' && /GATEWAY mode/.test(r.body.verdict || ''), r.body.verdict);
  r = await post(`/api/bridges/${BR.gt}/probe`, { port: P.gSpare, unit_id: 1 });
  check('Check mode on a closed port: says the box does not accept connections',
    /does not accept connections/.test(r.body.verdict || '') && r.body.error, r.body.verdict);
  check('devices on the transparent bus survive the wrong-type probes',
    await allFreshSince([DEV.u1, DEV.u2], Date.now() / 1000 + 1, 15));
  for (const id of [BR.tg, BR.gt]) await del(`/api/bridges/${id}`);

  // ═══ 5. A Modbus TCP gateway and a device behind it ═════════════════════
  await addBridge({ type: 'modbus_gateway', name: 'E2E gateway', id: BR.g, host: '127.0.0.1',
    ports: [{ port: P.g, label: 'gw bus' }] });
  await card(BR.g).waitFor();
  check('gateway bridge appears on the Devices page', (await card(BR.g).innerText()).includes(`:${P.g}`));
  await busButton(BR.g, P.g, 'addDeviceOnBus');
  await page.waitForSelector('#devWizGw');
  check('wizard on a gateway bus opens as Modbus TCP, reached through that gateway',
    await page.isChecked('input[name="devWizProto"][value="tcp"]') && await page.inputValue('#devWizGw') === `${BR.g}::${P.g}`,
    await page.inputValue('#devWizGw'));
  await page.fill('#devWizUnit', '2');
  const tg = await (async () => {
    const out = page.locator('#devWizTestResult');
    await out.evaluate(el => { el.textContent = ''; });
    await page.click('#devWizTestBtn');
    await page.waitForFunction(() => document.getElementById('devWizTestResult')?.textContent.trim(), null, { timeout: 20000 });
    return (await out.textContent()).trim();
  })();
  check('wizard Test (gateway, unit 2) succeeds', /^✓/.test(tg), tg);
  await wizFinish(DEV.g2, 'E2E behind gateway');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });
  v = await waitValues(DEV.g2, WANT.g2);
  check('device behind the gateway reads 7777 / 8888 (unit 2)', shown(v) === WANT.g2.join(), shown(v));
  const g2row = (await api('/api/devices')).body.devices.find(d => d.id === DEV.g2) || {};
  check('…its protocol resolves to Modbus TCP', g2row.protocol === 'tcp' && g2row.bridge === BR.g,
    `${g2row.protocol} via ${g2row.bridge}`);
  r = await post('/api/devices', { id: DEV.g2b, name: 'dup on gateway', template: TPL, enabled: true,
    connection: { bridge: BR.g, bridge_port: P.g, unit_id: 2 } });
  bug('the same unit id twice on a GATEWAY bus is refused (422) like on a transparent one',
    r.status === 422, `HTTP ${r.status}: the duplicate check in api.py _validate_device_payload only runs for protocol rtu-tcp`);
  if (r.status !== 422) await del(`/api/devices/${DEV.g2b}`);

  // ═══ 6. Edit a bridge: its devices follow ═══════════════════════════════
  // 6a. the bus's TCP port changes (the converter was reconfigured): the sim
  //     is started on the new port and the bus row edited in the modal
  startSim('transparent', P.t2);
  await sleep(1500);
  await goDevices();
  await card(BR.t).locator('[data-action="openBridgeModal"]').click();
  await page.waitForSelector('#bridgeModal.active');
  check('edit modal: type and id are locked', await page.isDisabled('#brId')
    && await page.locator('#bridgeBody input[name="brType"]:checked').isDisabled());
  await page.locator('#bridgeBody .br-port').first().fill(String(P.t2));
  await closeToasts();
  await page.click('#bridgeModal [data-action="saveBridge"]');
  await page.waitForSelector('#bridgeModal', { state: 'hidden' });
  const portToast = await lastToast();
  await sleep(4000);
  const moved = (await api('/api/devices')).body.devices.filter(d => [DEV.u1, DEV.u2].includes(d.id));
  const s2 = await freshStats(P.t2);
  bug('changing a bus port moves its devices to the new port',
    moved.every(d => +d.port === P.t2) && s2.accepts > 0,
    `toast "${portToast.slice(0, 90)}"; devices still on ${moved.map(d => d.port).join(',')}, new-port sim accepts=${s2.accepts}; `
    + `the bridge now shows a phantom bus :${P.t} (devices' bridge_port is the port NUMBER, not a stable bus key — bridges.py/_view re-adds it)`);
  b = (await api(`/api/bridges/${BR.t}`)).body;
  const phantom = (b.ports || []).find(p => +p.port === P.t);
  if (phantom && !(await api(`/api/bridges/${BR.t}`)).body.ports.some(p => +p.port === P.t && p.label))
    note(`after the port edit the card lists bus :${P.t} (no label, not declared) holding the devices, and :${P.t2} empty`);
  // put the bus back (API) so the next step starts from a working bus
  r = await put(`/api/bridges/${BR.t}`, { name: 'E2E transparent', type: 'rtu_transparent', host: '127.0.0.1',
    ports: [{ port: P.t, label: 'panel A', serial: '9600 8N1' }, { port: P.tSpare, label: 'panel B' }] });
  check('bus port restored', r.status === 200, errText(r.body));
  stopSim(P.t2);

  // 6b. the converter gets another address: 127.0.0.1 → the container's LAN IP
  await goDevices();
  await card(BR.t).locator('[data-action="openBridgeModal"]').click();
  await page.waitForSelector('#bridgeModal.active');
  await page.fill('#brHost', CTR_IP);
  await closeToasts();
  const sinceMove = Date.now() / 1000;
  await page.click('#bridgeModal [data-action="saveBridge"]');
  await page.waitForSelector('#bridgeModal', { state: 'hidden' });
  const hostToast = await lastToast();
  check('saving a new host says its devices reconnected', new RegExp(DEV.u1).test(hostToast) && new RegExp(DEV.u2).test(hostToast),
    hostToast);
  const movedHost = (await api('/api/devices')).body.devices.filter(d => [DEV.u1, DEV.u2].includes(d.id));
  check('both devices now resolve to the new host', movedHost.length === 2 && movedHost.every(d => d.host === CTR_IP),
    movedHost.map(d => d.host).join(','));
  check('…and keep reading (fresh samples)', await allFreshSince([DEV.u1, DEV.u2], sinceMove + 1, 15));
  v = await waitValues(DEV.u2, WANT.t2, 5);
  check('…exact values after the move (slave 2: 3333 / 4444)', shown(v) === WANT.t2.join(), shown(v));
  await sleep(1500);
  const sMove = await freshStats(P.t);
  check('the converter now sees the gateway coming from the new address', sMove.peer === CTR_IP,
    `last peer ${sMove.peer}`);
  check('the card shows the new host', (await card(BR.t).innerText()).includes(CTR_IP));

  // ═══ 7. MBG serial bridge: Set-up command, offline with a reason ═══════
  // 192.0.2.1 is TEST-NET-1: never answers, so the health check times out
  await addBridge({ type: 'mbg_serial_bridge', name: 'E2E Pi', id: BR.s, host: '192.0.2.1' });
  await page.waitForSelector('#bridgeInfoModal.active', { timeout: 20000 });
  const run = await page.textContent('#brRun').catch(() => '');
  const tok = (run.match(/BRIDGE_TOKEN='([^']*)'/) || [])[1] || '';
  check('saving our serial bridge opens Set-up with a docker run carrying BRIDGE_TOKEN',
    /docker run/.test(run) && tok.length >= 20, run.slice(0, 160));
  const setup = (await api(`/api/bridges/${BR.s}/setup`)).body;
  check('…the same token the gateway keeps (auth off: shown)', setup.token === tok);
  check('API never shows the token on the bridge itself', (await api(`/api/bridges/${BR.s}`)).body.token === undefined
    && (await api(`/api/bridges/${BR.s}`)).body.has_token === true);
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));
  // health is asked in the background ("checking…" first): wait for the verdict
  let sHead = '';
  for (let i = 0; i < 12; i++) {
    await goDevices();
    sHead = await card(BR.s).locator('.bridge-head').innerText();
    if (!/checking/i.test(sHead)) break;
    await sleep(1000);
  }
  check('serial bridge card: offline with a reason', /offline/i.test(sHead) && /192\.0\.2\.1/.test(sHead), sHead);
  check('…and says to start it on its host (no bus seen yet)', /start the bridge on its host/i.test(await card(BR.s).innerText()));
  await card(BR.s).locator('[data-action="bridgeScan"]').click();
  await page.waitForSelector('#bridgeInfoModal.active', { timeout: 20000 });
  await page.waitForFunction(() => document.querySelector('#bridgeInfoBody .field-error'), null, { timeout: 20000 });
  check('Buses on an unreachable serial bridge: says it did not answer and what to check',
    /did not answer/.test(await page.innerText('#bridgeInfoBody')));
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));
  // the health check is synchronous: an unreachable box slows the whole list
  const tl0 = Date.now(); await sleep(10500); const tl1 = Date.now();
  await api('/api/bridges'); const listMs = Date.now() - tl1; void tl0;
  if (listMs > 2000)
    note(`GET /api/bridges takes ${listMs} ms when an MBG serial bridge is unreachable (3 s health timeout, cached 10 s) — the Devices list and the Add Device wizard wait for it`);

  // ═══ 8. Delete a bridge in use → refused ════════════════════════════════
  await goDevices();
  await closeToasts();
  await card(BR.t).locator('[data-action="deleteBridge"]').click();
  const delToast = await lastToast();
  check('deleting a bridge in use is refused, naming its devices',
    /Cannot delete/.test(delToast) && new RegExp(DEV.u1).test(delToast) && new RegExp(DEV.u2).test(delToast), delToast);
  check('…and it is still there', (await api(`/api/bridges/${BR.t}`)).status === 200);
  await closeToasts();
  await card(BR.s).locator('[data-action="deleteBridge"]').click();
  await page.waitForSelector(`.bridge-card[data-bridge="${BR.s}"]`, { state: 'detached' });
  check('deleting an unused bridge works from the card', (await api(`/api/bridges/${BR.s}`)).status === 404);

  // ═══ 9. Phone width ═════════════════════════════════════════════════════
  await page.setViewportSize({ width: 390, height: 844 });
  await goDevices();
  await page.waitForTimeout(400);
  const ov = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth,
    wide: [...document.querySelectorAll('.bridge-card, .bridge-card *')]
      .filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
      .map(e => e.className || e.tagName).slice(0, 4) }));
  check('Devices page with bridges at 390 px: no horizontal overflow',
    ov.doc <= ov.vw + 1 && !ov.wide.length, JSON.stringify(ov));
  await page.setViewportSize({ width: 1440, height: 950 });

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await cleanup();
  stopSims();
}
if (notes.length) console.log(`\nNotes (UX, not failures):\n- ${notes.join('\n- ')}`);
if (bugs.length) console.log(`\nKnown product bugs (${STRICT ? 'counted, STRICT=1' : 'not counted; STRICT=1 counts them'}):\n- ${bugs.join('\n- ')}`);
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
