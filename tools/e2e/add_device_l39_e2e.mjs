// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Add Device end to end (ADR-0031 §2, L3.9): What · Connection · Check ·
// Name & save, against simulators with KNOWN values, on a throwaway gateway.
// A  Modbus RTU over a transparent converter (sims/bridge_sims.py, slaves 1
//    and 2): model → port RS-485 → through a bridge → Modbus RTU → Master →
//    unit 1; Check refuses until "open the connection anyway" (nobody can say
//    who holds a converter), then reads 1111 / 2222 (quality ok); Name & save
//    shows the review strip from the dry run; the POST is today's create
//    payload, key for key; the result panel says "Created · reading ·
//    write-locked"; the device is stored write-locked and reads 1111
// B  Create another like this: unit 2 proposed and taken, the name the next
//    one, the Check through the first device's connection (no confirm), 3333
// C  MQTT with parts (a broker in the gateway's own network namespace, a GX's
//    retained topics): the values, "Found inside it" ticked, the device made
//    with its battery component in the same request
// D  HTTP Solar API (fake_solar_api.mjs): host + device id, PAC 4200 W read
// E  the measured path from a meter's page ("Add equipment this meter
//    measures…"), its meter row filled in
// F  a refusal in the dry run keeps Create aria-disabled with its reason (an
//    id that exists), and a Change link opens its step
// G  ways in: a bridge bus's + Device (Connection, the bus and the next free
//    address filled), Found → Use (an MQTT announcement → Check, pre-filled),
//    ⋯ Duplicate (Connection, the copy list, the next free address)
// H  the device page: Preview (header) and the Edit modal's Connection Preview
// I  keyboard only: Add, the model, the connection, Check, Create — no mouse
// J  a phone (390 px): full screen, nothing scrolls sideways, every step
// K  Romanian: the steps, "Înainte — fără verificare"
//
// Against a throwaway gateway of the qa fixture, login on (127.0.0.1 only):
//   MBG_FX_NAME=mbg-fx-l39 tools/e2e/fixture_up.sh qa 18391 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18391 CTR=mbg-fx-l39 node tools/e2e/add_device_l39_e2e.mjs
//   docker rm -f mbg-fx-l39 mbg-fx-l39-mqtt mbg-fx-l39-solar
// It starts the bridge sim inside the gateway, a broker and the Solar API
// stand-in in its network namespace, and removes everything e2e-l39* it made.
import { chromium, request as pwRequest } from 'playwright';
import { execFileSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceEditor, deviceAction } from './lib/device-page.mjs';
import * as AD from './lib/add-device.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18391';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const CTR = process.env.CTR || 'mbg-fx-l39';
const BR_PORT = 14198;
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : ''}`);
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const docker = args => execFileSync('docker', args, { encoding: 'utf8' });
const inCtr = cmd => docker(['exec', '-u', '10001', CTR, 'sh', '-c', cmd]);

const api = await pwRequest.newContext({ baseURL: BASE });
await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const get = async u => (await api.get(u)).json().catch(() => ({}));
const getDev = async id => ((await get(`/api/devices/${id}`)).device) || {};
const post = async (u, data) => { const r = await api.post(u, { data }); return { status: r.status(), body: await r.json().catch(() => ({})) }; };
const del = u => api.delete(u).catch(() => {});

const TPL = 'e2e_l39_tpl';
const BR = 'e2e-l39-br';
const IDS = ['e2e-l39-s1', 'e2e-l39-s2', 'e2e-l39-gx', 'e2e-l39-fro', 'e2e-l39-ups', 'e2e-l39-kb', 'e2e-l39-dup', 'e2e-l39-tas'];
async function cleanup() {
  const devs = (await get('/api/devices')).devices || [];
  for (const d of devs) if (/^e2e-l39/.test(d.id) && !d.component_of) await del(`/api/devices/${d.id}`);
  for (const id of IDS) await del(`/api/devices/${id}`);
  await del(`/api/bridges/${BR}`);
  await del(`/api/device-templates/${TPL}`);
}
function companions(start) {
  for (const n of [`${CTR}-mqtt`, `${CTR}-solar`]) { try { docker(['rm', '-f', n]); } catch { /* none */ } }
  if (!start) return;
  docker(['run', '-d', '--name', `${CTR}-mqtt`, '--network', `container:${CTR}`, 'eclipse-mosquitto:2', 'mosquitto', '-c', '/mosquitto-no-auth.conf']);
  docker(['run', '-d', '--name', `${CTR}-solar`, '--network', `container:${CTR}`, '-e', 'FAKE_PORT=18099',
          '-v', `${path.join(HERE, 'fake_solar_api.mjs')}:/fake/fake_solar_api.mjs:ro`, 'node:24-slim', 'node', '/fake/fake_solar_api.mjs']);
}
const pub = (topic, payload) => docker(['exec', `${CTR}-mqtt`, 'mosquitto_pub', '-r', '-t', topic, '-m', payload]);

// ── the stage ─────────────────────────────────────────────────────────────
try { inCtr(`touch /tmp/br_sim_${BR_PORT}.stop`); } catch { /* none */ }
await cleanup();
companions(true);
await sleep(1200);
inCtr(`rm -f /tmp/br_sim_${BR_PORT}.stop /tmp/br_sims.stop`);
docker(['cp', path.join(HERE, 'sims', 'bridge_sims.py'), `${CTR}:/tmp/bridge_sims.py`]);
docker(['exec', '-d', '-u', '10001', CTR, 'sh', '-c', `python3 /tmp/bridge_sims.py transparent ${BR_PORT} > /tmp/br_${BR_PORT}.log 2>&1`]);
await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E L39 slave', vendor: 'E2E', model: 'L39',
  protocol: { byte_order: 'big', transports: ['tcp', 'rtu'] },
  poll_groups: { normal: { interval: 1 } },
  registers: [
    { address: 0, name: 'reg_a', label: 'Reg A', data_type: 'uint16', poll_group: 'normal' },
    { address: 1, name: 'reg_b', label: 'Reg B', data_type: 'uint16', poll_group: 'normal' },
  ] } });
const brSave = await post('/api/bridges', { id: BR, name: 'E2E L39 converter', type: 'rtu_transparent', host: '127.0.0.1', ports: [{ port: BR_PORT }] });
check('stage: the converter is a bridge', brSave.status < 300, brSave);
// a GX on the broker: its battery service announced (retained), its values
// published live every second (a GX template takes no retained value)
docker(['exec', '-d', `${CTR}-mqtt`, 'sh', '-c',
  'while true; do mosquitto_pub -t N/e2eportal/system/0/Dc/Battery/Soc -m \'{"value": 55.5}\'; '
  + 'mosquitto_pub -t N/e2eportal/system/0/Dc/Battery/Power -m \'{"value": -120}\'; sleep 1; done']);
pub('N/e2eportal/battery/512/ProductName', '{"value": "SmartShunt 500A"}');
pub('N/e2eportal/battery/512/CustomName', '{"value": "House bank"}');
// a Tasmota plug announcing itself (Found on MQTT)
pub('tasmota/discovery/AABBCCDDEEFF/config', JSON.stringify({ t: 'e2e_l39_plug', dn: 'E2E L39 plug', hn: 'plug', md: 'Sonoff', ip: '127.0.0.9',
  ft: '%prefix%/%topic%/', tp: ['cmnd', 'stat', 'tele'], rl: [1], onln: 'Online', ofln: 'Offline', sw: '13' }));
// a loopback HTTP device is refused unless allowed (the stand-in lives there)
const sec = await get('/api/config/security');
await post('/api/config/security', Object.assign({}, sec, { allow_nonlan_http_devices: true }));

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  // a fetch cut by page.goto (a navigation of the test itself) is not the page's error
  page.on('pageerror', e => { if (!/Failed to fetch/.test(String(e))) page.errs.push(String(e)); });
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|409|412|422|429|Failed to fetch/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  if (opts.lang) await page.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, opts.lang);
  page.posts = [];
  page.on('request', r => { if (r.method() === 'POST' && /\/api\/devices(\?|$)/.test(r.url()) && !/dry_run/.test(r.url())) page.posts.push(r.postData()); });
  await page.goto(BASE + '/#/devices');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.waitForSelector('#devicesPage [data-action="openDeviceWizard"]', { state: 'visible' });
  return page;
}
const live = async (id, name) => {
  for (let i = 0; i < 24; i++) {
    const v = (await get(`/api/values?device=${id}`).catch(() => ({}))).values || {};
    const x = Object.values(v).find(e => e && e.name === name);
    if (x && x.value !== undefined && x.value !== null) return x.value;
    await sleep(500);
  }
  return null;
};
const valueOf = (c, n) => (c.values.find(v => v.name === n) || {}).value;

// ── A · Modbus RTU over a converter, end to end ─────────────────────────
let page = await open();
await AD.openAdd(page);
check('A: the Add button opens the new flow, the search focused', await page.evaluate(() => document.activeElement?.id) === 'adQ');
check('A: no flag, no old wizard in the page', !(await page.locator('#deviceWizardModal').count()));
let st = await AD.pickModel(page, 'L39', TPL);
check('A: a model picked → Connection', st === 'connection', st);
await AD.answer(page, 'PortKind', 'serial');
await AD.answer(page, 'Reach', 'bridge');
await page.selectOption('#csAddBus', `${BR}::${BR_PORT}`);
await page.waitForTimeout(400);
const roleIds = await page.evaluate(() => [...document.querySelectorAll('[name="csAddRole"]')].map(x => x.id + ':' + x.checked));
check('A: an unclaimed bus asks the role, none chosen for it', roleIds.length === 2 && roleIds.every(x => x.endsWith(':false')), roleIds);
await AD.answer(page, 'Role', 'poll');
await AD.fill(page, 'Unit', 1);
await AD.fill(page, 'Timeout', 1);
st = await AD.next(page);
check('A: Next asked the gateway (only=connection) and moved to Check', st === 'check', st);
let c = await AD.waitCheck(page);
check('A: a converter nobody can vouch for: refused, "open the connection anyway" offered',
      c.refused && (await page.locator('#adChkConfirmOpen').count()) === 1, c);
check('A: Next is still possible — "Next — without a check"', /without a check/.test(c.next), c.next);
await page.check('#adChkConfirmOpen');
await page.click('#adCheckAgain');
c = await AD.waitCheck(page);
check('A: confirmed: the slave\'s values read, quality ok', valueOf(c, 'reg_a') === '1111' && valueOf(c, 'reg_b') === '2222'
      && c.values.every(v => v.q === 'ok'), c);
check('A: a good check is a plain Next', c.next === 'Next', c.next);
st = await AD.next(page);
check('A: → Name & save', st === 'save', st);
await page.waitForSelector('#adReviewBody .ad-strip');
const strip = await page.locator('#adReviewBody').innerText();
check('A: the review strip from the dry run: wire, bus, outputs, writes locked',
      /Modbus/.test(strip) && /127\.0\.0\.1|e2e-l39|14198/.test(strip) && /mbg\/devices\//.test(strip) && /locked/.test(strip), strip);
check('A: each layer has its Change', (await page.locator('#adReviewBody [data-ad-goto]').count()) >= 5);
check('A: OPC UA off, said', /OPC UA · off/.test(await page.locator('#adOutOpcua').innerText()));
check('A: the Writes card', /write-locked/.test(await page.locator('#adWrites').innerText()));
await page.fill('#adId', 'e2e-l39-s1');
await page.locator('#adId').dispatchEvent('input');
let res = await AD.create(page, { name: 'L39 slave 1' });
check('A: the result panel: Created · reading · write-locked', /Created · reading · write-locked/.test(res), res);
const sent = JSON.parse(page.posts[page.posts.length - 1] || '{}');
check('A: the create is today\'s payload, key for key, then how the check ended (L3.9-F3)',
      JSON.stringify(Object.keys(sent)) === JSON.stringify(['id', 'name', 'template', 'enabled', 'connection', 'mqtt', 'influxdb', 'ha_discovery_enabled', 'check', 'check_job'])
      && sent.check === 'ok' && sent.connection.bridge === BR && sent.connection.unit_id === 1 && sent.name === 'L39 slave 1', sent);
const madeLine = ((await get('/api/audit?limit=200')).entries || []).find(e => e.action === 'device created' && e.target === 'e2e-l39-s1');
check('A: the audit records the create with its check (ok, the job)', !!madeLine
      && /"check": ?"ok"/.test(madeLine.detail) && madeLine.detail.includes(sent.check_job), madeLine);
let dev = await getDev('e2e-l39-s1');
check('A: stored write-locked, on the bridge, unit 1', dev.write_locked === true && dev.bridge === BR && +dev.unit_id === 1, { wl: dev.write_locked, br: dev.bridge, u: dev.unit_id });
check('A: it reads 1111', +(await live('e2e-l39-s1', 'reg_a')) === 1111);
const anotherLabel = await page.locator('#adResAnother').innerText();
check('A: "Add another like this (unit 2)"', /unit 2/.test(anotherLabel), anotherLabel);

// ── B · Create another like this ────────────────────────────────────────
await page.click('#adResAnother');
await page.waitForFunction(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep === 'connection');
await page.waitForFunction(() => document.getElementById('csAddUnit')?.value === '2', null, { timeout: 10000 }).catch(() => {});
check('B: back on Connection, the next free address taken', await page.inputValue('#csAddUnit') === '2', await page.inputValue('#csAddUnit').catch(() => ''));
check('B: the bus kept, Master kept', await page.inputValue('#csAddBus') === `${BR}::${BR_PORT}` && await page.isChecked('#csAddRole-poll'));
st = await AD.next(page);
c = await AD.waitCheck(page);
check('B: the Check rides the first device\'s connection (no confirm): 3333 / 4444', valueOf(c, 'reg_a') === '3333' && valueOf(c, 'reg_b') === '4444', c);
await AD.next(page);
check('B: the name proposed is the next one', await page.inputValue('#adName') === 'L39 slave 2', await page.inputValue('#adName'));
await page.fill('#adId', 'e2e-l39-s2');
await page.locator('#adId').dispatchEvent('input');
res = await AD.create(page, {});
dev = await getDev('e2e-l39-s2');
check('B: created on the same bus, unit 2, reading 3333', dev.bridge === BR && +dev.unit_id === 2 && +(await live('e2e-l39-s2', 'reg_a')) === 3333, dev);
await page.click('#adResDone');
check('A/B: no page errors', page.errs.length === 0, page.errs);

// ── C · MQTT with parts ─────────────────────────────────────────────────
await AD.openAdd(page);
await AD.pickModel(page, 'victron gx', 'victron_gx_mqtt');
await page.waitForSelector('#csAddBroker');
await AD.fill(page, 'Broker', '127.0.0.1');
await page.evaluate(() => { const t = document.getElementById('csAddMqttTopic'); if (t) { t.value = 'N/e2eportal/#'; t.dispatchEvent(new Event('input', { bubbles: true })); } });
await page.evaluate(() => { const t = document.getElementById('csAddMqttVars'); if (t) { t.value = 'portal=e2eportal'; t.dispatchEvent(new Event('input', { bubbles: true })); } });
st = await AD.next(page);
check('C: MQTT connection → Check', st === 'check', [st, await page.locator('#addDevErrors').innerText()]);
c = await AD.waitCheck(page, 40000);
check('C: a GX value read (SOC 55.5)', valueOf(c, 'soc') === '55.5', c.values.slice(0, 4));
check('C: "Found inside it": the battery, ticked', c.comps.length === 1 && c.comps[0] === true, c.comps);
check('C: its name from the GX', await page.inputValue('#adCompName-0') === 'House bank');
await AD.next(page);
await page.fill('#adId', 'e2e-l39-gx');
await page.locator('#adId').dispatchEvent('input');
await page.waitForSelector('#adReviewBody [data-layer="inside"]', { timeout: 15000 }).catch(() => {});
check('C: the review strip lists what comes with it', /House bank/.test(await page.locator('#adReviewBody').innerText()));
res = await AD.create(page, { name: 'L39 GX' });
check('C: created, with 1 part', /1 part/.test(await page.locator('#adResultState').innerText()), await page.locator('#adResultState').innerText());
const sentGx = JSON.parse(page.posts[page.posts.length - 1] || '{}');
check('C: the components in the same request', (sentGx.components || []).length === 1 && sentGx.components[0].type === 'battery'
      && String(sentGx.components[0].instance) === '512' && sentGx.components[0].name === 'House bank', sentGx.components);
const comps = (await get('/api/devices')).devices.filter(d => d.component_of === 'e2e-l39-gx');
check('C: its battery exists, a component of it', comps.length === 1, comps.map(d => d.id));
await page.click('#adResDone');

// ── C2 · the check reset (review M1): a GX checked, Back, another broker,
// Next without a check — the parts and the nameplate of the first check are
// not posted ──
await AD.openAdd(page);
await AD.pickModel(page, 'victron gx', 'victron_gx_mqtt');
await page.waitForSelector('#csAddBroker');
await AD.fill(page, 'Broker', '127.0.0.1');
await page.evaluate(() => { for (const [id, v] of [['csAddMqttTopic', 'N/e2eportal/#'], ['csAddMqttVars', 'portal=e2eportal']]) {
  const t = document.getElementById(id); if (t) { t.value = v; t.dispatchEvent(new Event('input', { bubbles: true })); } } });
await AD.next(page);
c = await AD.waitCheck(page, 40000);
check('C2: the first check found the battery', c.comps.length === 1, c.comps);
await page.click('#addDevBack');
await page.waitForSelector('#csAddMqttPort');
await AD.fill(page, 'MqttPort', '1884');                  // nothing listens there
await AD.next(page);
c = await AD.waitCheck(page, 40000);
check('C2: another broker: the check starts afresh and finds nothing', c.comps.length === 0 && /without a check/.test(c.next), c);
await AD.next(page);
await page.fill('#adId', 'e2e-l39-gx2');
await page.locator('#adId').dispatchEvent('input');
await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 }).catch(() => {});
await page.click('#addDevNext');
await page.waitForSelector('#adResult', { timeout: 20000 }).catch(() => {});
const sentGx2 = JSON.parse(page.posts[page.posts.length - 1] || '{}');
check('C2: the create posts no part and no nameplate of the first check, and says it was not checked',
      !('components' in sentGx2) && !((sentGx2.attributes || {}).identity) && sentGx2.check === 'failed', sentGx2);
await page.click('#adResDone').catch(() => {});

// ── C3 · a check left while it starts is cancelled (review M2) ──
{
  const seen = { del: [], job: '' };
  page.on('request', r => { if (r.method() === 'DELETE' && /\/api\/previews\//.test(r.url())) seen.del.push(r.url()); });
  await page.route('**/api/previews', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const resp = await route.fetch();
    const body = await resp.json().catch(() => ({}));
    seen.job = body.job || '';
    await sleep(1500);                                    // the answer is late: the dialog closes before it
    await route.fulfill({ response: resp, json: body });
  });
  await AD.openAdd(page);
  await AD.pickModel(page, 'victron gx', 'victron_gx_mqtt');
  await page.waitForSelector('#csAddBroker');
  await AD.fill(page, 'Broker', '127.0.0.1');
  await page.evaluate(() => { for (const [id, v] of [['csAddMqttTopic', 'N/e2eportal/#'], ['csAddMqttVars', 'portal=e2eportal']]) {
    const t = document.getElementById(id); if (t) { t.value = v; t.dispatchEvent(new Event('input', { bubbles: true })); } } });
  await page.click('#addDevNext');
  await page.waitForFunction(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep === 'check', null, { timeout: 10000 }).catch(() => {});
  await page.evaluate(() => { window.app._ad.done = true; window.app.closeModal('addDeviceModal'); });
  await sleep(3500);
  await page.unroute('**/api/previews');
  const st = seen.job ? (await get(`/api/previews/${seen.job}`)).state : '';
  check('C3: closed while the check started: its job is cancelled (DELETE sent, the job says cancelled)',
        seen.job && seen.del.some(u => u.endsWith(seen.job)) && st === 'cancelled', { job: seen.job, del: seen.del, st });
  const again = await post('/api/previews', { connection: { protocol: 'mqtt', broker: '127.0.0.1', port: 1883, topic: 'N/e2eportal/#', vars: { portal: 'e2eportal' } },
                                              template: 'victron_gx_mqtt', seconds: 2 });
  check('C3: …and the bus is free: a new preview starts', again.status === 202, again);
  if (again.body.job) await del(`/api/previews/${again.body.job}`);
}

// ── D · HTTP Solar API ──────────────────────────────────────────────────
await AD.openAdd(page);
await AD.pickModel(page, 'fronius inverter solar api', 'fronius_solar_api_inverter');
await AD.fill(page, 'HttpHost', '127.0.0.1:18099');
await AD.fill(page, 'HttpId', '1');
st = await AD.next(page);
c = await AD.waitCheck(page);
if (c.state === 'timeout') console.log('D:', JSON.stringify(c), JSON.stringify(await page.evaluate(() => ({ host: document.getElementById('csAddHttpHost')?.value, id: document.getElementById('csAddHttpId')?.value, n: document.querySelectorAll('#csAddHttpHost').length, M: window.app._cf.csAdd.M.url, mode: window.app._cf.csAdd.M.mode, tpl: (window.app._adTpl() || {}).http_path, live: !!window.app._ad.cs?.form?.host?.isConnected, same: window.app._ad.cs?.form === window.app._cf.csAdd }))));
check('D: the Solar API read: PAC 4200 W, quality ok', valueOf(c, 'power_active_total') === '4200'
      && (c.values.find(v => v.name === 'power_active_total') || {}).q === 'ok', c.values.slice(0, 3));
await AD.next(page);
await page.fill('#adId', 'e2e-l39-fro');
await page.locator('#adId').dispatchEvent('input');
res = await AD.create(page, { name: 'L39 Fronius' });
dev = await getDev('e2e-l39-fro');
check('D: created, its URL the template\'s call', /GetInverterRealtimeData\.cgi\?Scope=Device&DeviceId=1/.test(JSON.stringify(dev)), dev.http);
await page.click('#adResDone');

// ── F · the dry run refuses: Create waits, its reason beside it ─────────
await AD.openAdd(page);
await AD.pickModel(page, 'L39', TPL);
await AD.answer(page, 'PortKind', 'serial');
await AD.answer(page, 'Reach', 'bridge');
await page.selectOption('#csAddBus', `${BR}::${BR_PORT}`);
await page.waitForTimeout(400);
await AD.fill(page, 'Unit', 9);
await AD.next(page);
await AD.next(page);                                 // without a check
await page.fill('#adId', 'e2e-l39-s1');              // taken
await page.locator('#adId').dispatchEvent('input');
await page.waitForFunction(() => document.getElementById('adRefuse')?.textContent, null, { timeout: 10000 }).catch(() => {});
check('F: an id that exists: Create aria-disabled, the reason beside it',
      await page.getAttribute('#addDevNext', 'aria-disabled') === 'true' && /e2e-l39-s1|exists|taken/.test(await page.locator('#adRefuse').innerText()),
      await page.locator('#adRefuse').innerText());
await page.click('#adReviewBody [data-ad-goto="connection"]').catch(() => {});
await page.click('#addDevRail [data-shell-goto="connection"]').catch(() => {});
check('F: a Change link / the rail goes back to its step', await AD.step(page) === 'connection');
page.dlg.answer = true;
await page.keyboard.press('Escape');
await page.waitForSelector('#addDeviceModal.active', { state: 'detached' }).catch(() => {});
await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'), null, { timeout: 5000 }).catch(() => {});
check('F: Esc asked before dropping the answers', page.dlg.seen.some(d => /Discard/i.test(d.title || '')), page.dlg.seen.map(d => d.title));

// ── E · the measured path from a meter's page ───────────────────────────
await page.goto(BASE + '/#/devices/em1');
await page.waitForSelector('#ddvActions');
await openDeviceEditor(page, 'meters');
await page.waitForSelector('#mtrAddMeasured');
await page.click('#mtrAddMeasured');
await page.waitForSelector('#addDeviceModal.active');
await page.waitForSelector('#adKinds');
check('E: the measured path, its first step', await AD.step(page) === 'what');
await page.locator('#adKinds label:has(input[value="charger"])').click();
await AD.next(page);
await page.waitForFunction(() => document.getElementById('adMtrDev-0')?.value === 'em1', null, { timeout: 8000 }).catch(() => {});
check('E: the meter row already names this meter', await page.inputValue('#adMtrDev-0') === 'em1');
await page.selectOption('#adMtrRole-0', 'consumption').catch(() => {});
await page.waitForTimeout(500);
await page.selectOption('#adMtrPower-0', 'power_active_total').catch(() => {});
st = await AD.next(page);
check('E: → Name & save', st === 'save', [st, await page.locator('#addDevErrors').innerText()]);
await page.fill('#adName', 'L39 charger');
await page.fill('#adId', 'e2e-l39-ups');
await page.click('#addDevNext');
await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'), null, { timeout: 10000 }).catch(() => {});
dev = await getDev('e2e-l39-ups');
check('E: created, measured by em1', (dev.meters || []).some(m => m.device === 'em1'), dev.meters);

// ── G · ways in ─────────────────────────────────────────────────────────
await page.goto(BASE + '/?fresh=g#/devices');
await page.waitForLoadState('load');
await page.waitForSelector(`.bridge-card[data-bridge="${BR}"]`, { state: 'attached' });
const plusDev = page.locator(`.bridge-card[data-bridge="${BR}"] [data-action="addDeviceOnBus"]`).first();
if (await plusDev.isVisible()) await plusDev.click(); else await page.evaluate(([b, p]) => window.app.addDeviceOnBus(b, p), [BR, BR_PORT]);
await page.waitForSelector('#addDeviceModal.active');
await page.waitForFunction(() => document.getElementById('csAddUnit'), null, { timeout: 10000 }).catch(() => {});
await page.waitForTimeout(800);
check('G: a bus\'s + Device: Connection first (By connection), the bus chosen, the next free address',
      await AD.step(page) === 'connection' && await page.inputValue('#csAddBus').catch(() => '') === `${BR}::${BR_PORT}`
      && await page.inputValue('#csAddUnit') === '3', [await AD.step(page), await page.inputValue('#csAddUnit').catch(() => '')]);
await page.click('#addDevCancel');
await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'), null, { timeout: 5000 }).catch(() => {});

await AD.openAdd(page);
await page.waitForSelector('#adFoundChip:not([hidden])', { timeout: 10000 }).catch(() => {});
await page.click('#adFoundChip').catch(() => {});
const useBtn = page.locator('#adFoundPanel li:has-text("E2E L39 plug") [data-ad-use]');
if (await useBtn.count()) {
  await useBtn.click();
  await page.waitForFunction(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep === 'check', null, { timeout: 8000 }).catch(() => {});
  check('G: Found → Use lands on Check, the model and connection pre-filled', await AD.step(page) === 'check');
  await page.click('#addDevCancel');
  page.dlg.answer = true;
  await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'), null, { timeout: 5000 }).catch(() => {});
} else {
  check('G: Found lists the Tasmota plug announced on MQTT', false, await page.locator('#adFoundPanel').innerText().catch(() => 'no Found chip'));
  await page.click('#addDevCancel');
}

await page.goto(BASE + '/#/devices/e2e-l39-s1');
await page.waitForSelector('#ddvActions');
await deviceAction(page, '[data-ddv-act="duplicate"]');
await page.waitForSelector('#addDeviceModal.active');
await page.waitForFunction(() => document.getElementById('csAddUnit')?.value === '3', null, { timeout: 10000 }).catch(() => {});
check('G: ⋯ Duplicate → Connection, the same bus, the next free address (3)', await AD.step(page) === 'connection'
      && await page.inputValue('#csAddUnit') === '3', await page.inputValue('#csAddUnit').catch(() => ''));
await AD.next(page);
await AD.next(page);
check('G: Duplicate proposes the next name', await page.inputValue('#adName') === 'L39 slave 2', await page.inputValue('#adName'));
await page.click('#addDevCancel');
await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'), null, { timeout: 5000 }).catch(() => {});

// ── H · the device page's Preview ───────────────────────────────────────
await page.goto(BASE + '/#/devices/e2e-l39-s1');
await page.waitForSelector('#ddvHeadPreview');
await page.click('#ddvHeadPreview');
await page.waitForSelector('#previewModal.active');
const pvEnd = () => page.waitForFunction(() => document.querySelector('#previewModalBody .pv-status:not(.is-running)') || document.querySelector('#previewModalBody .pv-refused'), null, { timeout: 20000 });
await pvEnd();
// the Duplicate just left may still be reading this bus: "busy — try again", then Run again
for (let i = 0; i < 4 && await page.locator('#previewModalBody .pv-refused.is-busy').count(); i++) {
  check('H: a preview already on this bus is said "busy — try again"', /try again/i.test(await page.locator('#previewModalBody').innerText()));
  await sleep(3000);
  await page.click('#previewModalAgain');
  await sleep(300);
  await pvEnd();
}
check('H: the header Preview reads it through its own connection (1111)', /1111/.test(await page.locator('#previewModalBody').innerText()),
      await page.locator('#previewModalBody').innerText());
await page.click('#previewModal [data-action="closeModal"]');
await openDeviceEditor(page, 'connection');
await page.waitForSelector('#devEditPreview');
await page.fill('#ddvUnit', '9');                   // a unit nobody answers on
await page.locator('#ddvUnit').blur();
await page.click('#devEditPreview');
await page.waitForSelector('#previewModal.active');
await page.waitForFunction(() => document.querySelector('#previewModalBody .pv-status:not(.is-running)') || document.querySelector('#previewModalBody .pv-refused'), null, { timeout: 20000 });
check('H: the Edit modal\'s Preview reads the EDITED connection (unit 9: no answer, not unit 1\'s 1111), nothing saved',
      /Nothing answered|no answer/.test(await page.locator('#previewModalBody').innerText()) && !/1111/.test(await page.locator('#previewModalBody').innerText())
      && +(await getDev('e2e-l39-s1')).unit_id === 1,
      await page.locator('#previewModalBody').innerText());
await page.click('#previewModal [data-action="closeModal"]');
check('C–H: no page errors', page.errs.length === 0, page.errs);
await page.context().close();

// ── I · keyboard only ───────────────────────────────────────────────────
page = await open();
await page.locator('#devicesPage [data-action="openDeviceWizard"]').first().focus();
await page.keyboard.press('Enter');
await page.waitForSelector('#addDeviceModal.active');
await page.waitForFunction(() => document.activeElement?.id === 'adQ');
await page.keyboard.type('fronius solar api');
await page.keyboard.press('ArrowDown');              // into the models
for (let i = 0; i < 8; i++) {
  if (await page.evaluate(() => document.activeElement?.dataset.adTpl) === 'fronius_solar_api_inverter') break;
  await page.keyboard.press('ArrowDown');
}
check('I: the arrows move through the models', await page.evaluate(() => document.activeElement?.dataset.adTpl) === 'fronius_solar_api_inverter');
await page.keyboard.press('Enter');                  // picked, and Next
await page.waitForFunction(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep === 'connection');
await page.waitForSelector('#csAddHttpHost');
await page.focus('#csAddHttpHost');
await page.keyboard.type('127.0.0.1:18099');
await page.keyboard.press('Tab');
await page.keyboard.type('2');
await page.keyboard.press('Enter');                  // Next
await page.waitForFunction(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep === 'check', null, { timeout: 10000 }).catch(() => {});
check('I: Enter in a field goes on (→ Check)', await AD.step(page) === 'check');
c = await AD.waitCheck(page);
check('I: the focus is on the step\'s heading, the check read', await page.evaluate(() => document.activeElement?.id) === 'addDevH-check' && c.values.length > 0,
      await page.evaluate(() => document.activeElement?.id));
await page.focus('#addDevNext');
await page.keyboard.press('Enter');
await page.waitForSelector('#adName');
await page.focus('#adId');
await page.keyboard.press('Control+A');
await page.keyboard.type('e2e-l39-kb');
await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 });
await page.waitForTimeout(600);
await page.keyboard.press('Enter');                  // Create
await page.waitForSelector('#adResult', { timeout: 15000 }).catch(() => {});
check('I: created from the keyboard; "Open it" focused', (await page.locator('#adResult').count()) === 1
      && await page.evaluate(() => document.activeElement?.id) === 'adResOpen', await page.evaluate(() => [document.getElementById('adResult') ? 'result' : 'no result', document.activeElement?.id, document.getElementById('addDevErrors')?.innerText, document.getElementById('adRefuse')?.innerText].join(' / ')));
await page.keyboard.press('Tab');
await page.keyboard.press('Tab');
await page.keyboard.press('Enter');                  // Done
await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'), null, { timeout: 5000 }).catch(() => {});
check('I: Done closes it', !(await page.locator('#addDeviceModal.active').count()));
await page.context().close();

// ── J · a phone ─────────────────────────────────────────────────────────
page = await open({ viewport: { width: 390, height: 844 } });
await AD.openAdd(page);
const wide = async () => page.evaluate(() => {
  const m = document.querySelector('#addDeviceModal .modal-content');
  return { doc: document.scrollingElement.scrollWidth, m: m.scrollWidth, w: m.clientWidth, full: Math.round(m.getBoundingClientRect().width) };
});
let w = await wide();
check('J: full screen on a phone', w.full === 390, w);
await AD.pickModel(page, 'L39', TPL);
await AD.answer(page, 'PortKind', 'serial');
await AD.answer(page, 'Reach', 'bridge');
await page.selectOption('#csAddBus', `${BR}::${BR_PORT}`);
await page.waitForTimeout(400);
w = await wide();
check('J: Connection: nothing scrolls sideways', w.m <= w.w + 1 && w.doc <= 391, w);
await AD.next(page);
await AD.waitCheck(page);
w = await wide();
check('J: Check: nothing scrolls sideways', w.m <= w.w + 1, w);
await AD.next(page);
await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 }).catch(() => {});
w = await wide();
check('J: Name & save: nothing scrolls sideways', w.m <= w.w + 1, w);
check('J: the rail is one line', /Step 4 of 4/.test(await page.locator('#addDevStepLine').innerText()), await page.locator('#addDevStepLine').innerText());
if (process.env.OUT) await page.screenshot({ path: path.join(process.env.OUT, 'l39-phone-save.png'), fullPage: false });
await page.context().close();

// ── K · Romanian ────────────────────────────────────────────────────────
page = await open({ lang: 'ro' });
await AD.openAdd(page);
const rail = await page.locator('#addDevRail').innerText();
check('K: the steps in Romanian', /Verificare/.test(rail) && /Nume/.test(rail), rail);
await AD.pickModel(page, 'sdm120', 'eastron_sdm120');
await AD.answer(page, 'PortKind', 'ethernet');
await AD.fill(page, 'Host', '127.0.0.1');
await AD.fill(page, 'Port', '15599');
await AD.next(page);
c = await AD.waitCheck(page);
check('K: "Înainte — fără verificare"', /Înainte — fără verificare/.test(c.next), c.next);
await AD.next(page);
await page.waitForSelector('#adWrites');
check('K: the Writes card in Romanian', /blocate/.test(await page.locator('#adWrites').innerText()), await page.locator('#adWrites').innerText());
check('A–K: no page errors (phone, Romanian)', page.errs.length === 0, page.errs);
await page.context().close();

// ── the end ─────────────────────────────────────────────────────────────
await browser.close();
await cleanup();
await post('/api/config/security', sec);
try { inCtr(`touch /tmp/br_sim_${BR_PORT}.stop`); } catch { /* gone */ }
companions(false);
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
