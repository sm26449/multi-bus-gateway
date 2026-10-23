// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The connection form on the paths around a save: off a gateway's bus with
// the port it shows, Test after taking a device off a bridge and on an edited
// bridged device, the keyboard on the segments, Back in Add Device keeping a
// typed address, the components lock, a local tap, a device measured only by
// its meters and an HTTP device saved untouched. Against the unchanged_save
// fixture (it adds and edits devices; restart the sandbox after).
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 node tools/e2e/connform_review_e2e.mjs
import { chromium } from 'playwright';
import { openDeviceTab } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const J = { 'Content-Type': 'application/json' };
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: J, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const row = async id => (await api('/api/devices')).body.devices.find(d => d.id === id);

// what this script adds: a local tap (the seplos shape), an HTTP device at an
// address nothing answers on, and a device measured only by its meters
const ADD = [
  { id: 'cr-tap', name: 'Bank tap', template: 'seplos_bms_v3_rtu_tap', enabled: false,
    connection: { protocol: 'rtu_tap', serial_port: '/dev/ttyFAKETAP', baudrate: 19200, parity: 'N', unit_id: 1 } },
  { id: 'cr-web', name: 'Inverter JSON', template: 'fronius_solar_api_inverter', enabled: false,
    connection: { protocol: 'http', url: 'http://10.255.255.1/solar_api/v1/GetInverterRealtimeData.cgi' } },
  { id: 'cr-in', name: 'In meter', template: 'eastron_sdm630', enabled: false,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15711, unit_id: 1 } },
  { id: 'cr-out', name: 'Out meter', template: 'eastron_sdm630', enabled: false,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15712, unit_id: 1 } },
  { id: 'cr-ups', name: 'UPS', kind: 'ups', connection: { protocol: 'none' },
    meters: [{ role: 'input', device: 'cr-in', power: 'power_active_total' },
             { role: 'output', device: 'cr-out', power: 'power_active_total' }] },
];

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open() {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.reqs = [];
  page.on('request', r => { if (r.method() !== 'GET') page.reqs.push({ method: r.method(), url: r.url(), body: r.postData() }); });
  await watchDialogs(page);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const putsTo = (page, from, id) => page.reqs.slice(from).filter(r => r.method === 'PUT' && r.url.endsWith(`/api/devices/${id}`));
async function connTab(page, id) {
  await page.evaluate(i => window.app.openDeviceDetail(i), id);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvConn fieldset, [data-dpanel="connection"] #ddvName', { state: 'attached' });
}
async function save(page) {
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForFunction(() => (document.getElementById('ddvFeedback')?.textContent || '').trim()
    || document.querySelector('#ddvConn [aria-invalid="true"]'), null, { timeout: 10000 });
  await page.waitForTimeout(300);
  return (await page.locator('#ddvFeedback').innerText().catch(() => '')).trim();
}
async function test(page) {
  const n = page.reqs.length;
  await page.click('#ddvConn [data-action="deviceDetailTest"]');
  await page.waitForFunction(() => (document.getElementById('ddvTestResult')?.textContent || '').trim(), null, { timeout: 20000 });
  const sent = page.reqs.slice(n).filter(r => r.method === 'POST' && /\/api\/devices\/(test|[^/]+\/test)$/.test(r.url));
  return { sent, adhoc: sent.find(r => /\/api\/devices\/test$/.test(r.url)), text: await page.innerText('#ddvTestResult') };
}

try {
  for (const d of ADD) {
    const r = await api('/api/devices', { method: 'POST', body: JSON.stringify(d) });
    if (r.status !== 200) throw new Error(`create ${d.id}: ${JSON.stringify(r.body)}`);
  }
  const page = await open();

  // ── MAJOR-1: off a gateway's bus, the port shown is the port saved ──
  await connTab(page, 'gwm');
  await page.selectOption('#ddvGw', '');
  check('off the gateway bus: the port shows 502', (await page.inputValue('#ddvPort')) === '502');
  check('…and the move is said, not "starts afresh"', await page.isVisible('#ddvRouteHint')
        && /leaves the bridge/.test(await page.innerText('#ddvRouteHint')), await page.innerText('#ddvRouteHint'));
  await page.fill('#ddvHost', '127.0.0.9');
  {
    const n = page.reqs.length;
    const fb = await save(page);
    const conn = JSON.parse(putsTo(page, n, 'gwm')[0]?.body || '{}').connection || {};
    check('off the gateway bus with 502 left as shown: 502 sent, the bridge said left',
          conn.port === 502 && conn.bridge === null && conn.host === '127.0.0.9' && conn.protocol === 'tcp', `${JSON.stringify(conn)} · ${fb}`);
    const st = await row('gwm');
    check('…stored at its own address, port 502, off the bridge', !st.bridge && st.port === 502 && st.host === '127.0.0.9', JSON.stringify(st.connection));
  }

  // ── MAJOR-4: Test on an edited bridged device tests the edited values ──
  await connTab(page, 'hp');
  await page.fill('#ddvUnitBr', '7');
  {
    const t = await test(page);
    const body = t.adhoc ? JSON.parse(t.adhoc.body) : {};
    check('an edited bridged device: Test sends the edited unit on its bus, not the saved device',
          t.sent.length === 1 && body.device_id === 'hp' && body.connection?.unit_id === 7 && body.connection?.bridge === 'ws',
          t.sent.map(r => r.url + ' ' + r.body).join(' | '));
  }

  // ── MAJOR-2: Test after "Take it off the bridge" says the bridge is left ──
  await connTab(page, 'hp');
  await page.click('#ddvConn a:has-text("Take it off the bridge")');
  await page.waitForSelector('#ddvRtuHost');
  await page.fill('#ddvRtuHost', '127.0.0.7');
  await page.fill('#ddvRtuPort', '4005');
  await page.locator('#ddvRtuPort').dispatchEvent('change');
  check('off the bridge: the move is said (the rest is kept)', /leaves the bridge/.test(await page.innerText('#ddvRouteHint')));
  {
    const t = await test(page);
    const body = t.adhoc ? JSON.parse(t.adhoc.body) : {};
    check('Test off the bridge: bridge null and the typed address',
          body.connection?.bridge === null && body.connection?.host === '127.0.0.7' && body.connection?.port === 4005, t.adhoc?.body);
    check('…and no "reaches its bus through bridge" refusal', !/through bridge/.test(t.text), t.text);
  }

  // ── MAJOR-3: the keyboard on the segments keeps its place ──
  await connTab(page, 'udp');
  await page.focus('#ddvProto-tcp');
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(200);
  let f = await page.evaluate(() => ({ id: document.activeElement?.id, on: document.activeElement?.checked }));
  check('ArrowRight on the protocol: focus stays on the radio now chosen', f.id === 'ddvProto-rtu' && f.on, JSON.stringify(f));
  await page.focus('#ddvConn input[name="ddvRtuMode"]:checked');
  const from = await page.evaluate(() => document.activeElement?.id);
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(200);
  f = await page.evaluate(() => ({ id: document.activeElement?.id, on: document.activeElement?.checked }));
  check('ArrowRight on the RTU mode: focus on the next mode, chosen', /^ddvRtuMode-/.test(f.id || '') && f.id !== from && f.on, JSON.stringify(f));

  // ── MINOR-5: a device holding components keeps how it is reached, everywhere ──
  for (const [id, sel] of [['gwm', '#ddvGw'], ['udp', '#ddvUdp'], ['cr-tap', '#ddvTapWhere']]) {
    await connTab(page, id);
    const ok = await page.evaluate(([i, s]) => {
      const CM = window.ConnformModel;
      const r = window.app._devDetail.entry;
      window.app._cfMount(document.getElementById('ddvConn'), 'ddv', CM.model(r), { context: 'edit', deviceId: i, lockMode: true, lockClass: 'modbus' });
      const el = document.querySelector(s);
      return el ? el.disabled : 'absent';
    }, [id, sel]);
    check(`holding components: ${sel} is held too (${id})`, ok === true, String(ok));
  }
  await connTab(page, 'hp');
  await page.evaluate(() => {
    const CM = window.ConnformModel;
    window.app._cfMount(document.getElementById('ddvConn'), 'ddv', CM.model(window.app._devDetail.entry), { context: 'edit', deviceId: 'hp', lockMode: true, lockClass: 'modbus' });
  });
  check('holding components: no "Take it off the bridge"', (await page.locator('#ddvConn a:has-text("Take it off the bridge")').count()) === 0);

  // ── a local tap (the seplos shape): untouched, then one field ──
  await connTab(page, 'cr-tap');
  {
    let n = page.reqs.length;
    let fb = await save(page);
    check('local tap: an untouched Save sends nothing', !putsTo(page, n, 'cr-tap').length && /Nothing to save/.test(fb), fb);
    await page.selectOption('#ddvBaud', '9600');
    n = page.reqs.length;
    fb = await save(page);
    const conn = JSON.parse(putsTo(page, n, 'cr-tap')[0]?.body || '{}').connection;
    check('local tap: the baud rate, as a tap', eq(conn, { baudrate: 9600, protocol: 'rtu_tap' }), `${JSON.stringify(conn)} · ${fb}`);
    const st = await row('cr-tap');
    check('…stored, still on its serial port', st.serial?.serial_port === '/dev/ttyFAKETAP' && st.serial?.baudrate === 9600, JSON.stringify(st.serial));
  }

  // ── a device measured only by its meters: an untouched Identity save ──
  await connTab(page, 'cr-ups');
  {
    const n = page.reqs.length;
    const fb = await save(page);
    check('meter-only: an untouched Save sends nothing', !putsTo(page, n, 'cr-ups').length && /Nothing to save/.test(fb), fb);
  }

  // ── HTTP: an untouched Save sends nothing ──
  await connTab(page, 'cr-web');
  {
    const n = page.reqs.length;
    const fb = await save(page);
    check('HTTP: an untouched Save sends nothing', !putsTo(page, n, 'cr-web').length && /Nothing to save/.test(fb), fb);
  }

  // ── masked topic: the toggle is named, the reason is said ──
  await connTab(page, 'gx');
  const lbl = await page.getAttribute('#ddvMqttTopic + button', 'aria-label');
  check('the topic stays masked, its toggle says what it shows', (await page.getAttribute('#ddvMqttTopic', 'type')) === 'password'
        && /Topic/.test(lbl || ''), lbl);
  check('…and why it is hidden is said beside it', /portal id/.test(await page.innerText('#ddvMqttMaskHint'))
        && (await page.getAttribute('#ddvMqttTopic', 'aria-describedby') || '').includes('ddvMqttMaskHint'));

  // ── MINOR-2: a field's error joins its hints, never replaces them ──
  await page.fill('#ddvMqttTopic', '');
  await save(page);
  const desc = (await page.getAttribute('#ddvMqttTopic', 'aria-describedby')) || '';
  check('an error on the topic keeps its hints described', desc.includes('ddvMqttTopicHint') && desc.includes('-err'), desc);

  // ── MAJOR-5: Back in Add Device keeps a typed RTU-over-network address ──
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#devWizConn fieldset');
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  check('wizard RTU: over the network, on the first bridge', await page.evaluate(() => window.app._cf.devWiz.M.bridge) === 'ws');
  await page.click('#devWizConn a:has-text("scan a bridge")');
  await page.fill('#devWizRtuHost', '127.0.0.7');
  await page.fill('#devWizRtuPort', '4005');
  await page.click('#devWizNext');
  await page.waitForFunction(() => window.app._devWiz?.step === 2);
  await page.click('#devWizBack');
  await page.waitForSelector('#devWizConn fieldset');
  const back = await page.evaluate(() => ({ wire: window.ConnformModel.wire(window.app._cf.devWiz.M),
                                            host: document.getElementById('devWizRtuHost')?.value }));
  check('Back: still at the typed address, never moved onto a bridge',
        !back.wire.bridge && back.wire.host === '127.0.0.7' && back.wire.port === 4005 && back.host === '127.0.0.7', JSON.stringify(back));
  check('the "Meters only" choice is short, its meaning said below',
        (await page.innerText('#deviceWizardModal label:has(input[value="none"])')).trim() === 'Meters only'
        && /measured by meters/.test(await page.innerText('#devWizConn')));
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));

  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  await page.close();
} catch (e) {
  check('ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
