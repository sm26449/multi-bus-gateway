// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// New-user friction fixed in 3.91.0, as a user meets it:
//  - a device behind a Modbus TCP gateway is added as Modbus TCP ("Reached:
//    through the gateway"), not under RTU; the RTU picker lists only RTU bridges;
//  - editing a Modbus device can switch among the Modbus variants;
//  - a disabled "Add instance" (Virtual Meters) says why.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node ux_gaps_e2e.mjs
import { chromium } from 'playwright';
import { saveDeviceEditor } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';
import { openAdd, pickModel, answer, fill, next } from './lib/add-device.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const TPL = 'e2e_ux_tpl';
const BR = { gw: 'e2e-ux-gw', rt: 'e2e-ux-rt' };
const DEV = { gw: 'e2e-ux-gwdev', tcp: 'e2e-ux-tcp', udp: 'e2e-ux-udp', asc: 'e2e-ux-ascii' };

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
async function cleanup() {
  for (const id of Object.values(DEV)) await del(`/api/devices/${id}`);
  for (const id of Object.values(BR)) await del(`/api/bridges/${id}`);
  await del(`/api/device-templates/${TPL}`);
}

await cleanup();
const tplSave = await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E UX slave', vendor: 'E2E',
  protocol: { byte_order: 'big', transports: ['tcp', 'rtu'] },
  poll_groups: { normal: { interval: 5 } },
  registers: [{ address: 0, name: 'reg_a', label: 'Reg A', data_type: 'uint16', poll_group: 'normal' }] } });
await post('/api/bridges', { id: BR.gw, name: 'E2E gateway', type: 'modbus_gateway', host: '192.0.2.61',
                             ports: [{ port: 502, label: 'panel' }] });
await post('/api/bridges', { id: BR.rt, name: 'E2E transparent', type: 'rtu_transparent', host: '192.0.2.62',
                             ports: [{ port: 4196 }] });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const dlg = await watchDialogs(page);              // a review before Save (a bus move): answered yes
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
async function goDevices() {
  await page.click('[data-page="devices"]');
  if (!(await page.isVisible('#devicesListView')))
    await page.click('#deviceRegistersView [data-action="closeDeviceRegisters"]');
  await page.waitForSelector('#devicesListView', { state: 'visible' });
}
// Add Device (L3.9): from the Connection step on — the check (nothing at
// these addresses answers: Next goes on without it), then Name & save, off
async function finish(id) {
  await next(page);                                   // → Check
  await page.waitForFunction(() => document.querySelector('#adCheck .pv-refused, #adCheck .pv-status:not(.is-running)'), null, { timeout: 25000 }).catch(() => {});
  await next(page);                                   // → Name & save
  await page.fill('#adId', id);
  await page.fill('#adName', id);
  await page.locator('#adId').dispatchEvent('input');
  if (await page.isChecked('#adEnabled')) await page.click('label:has(#adEnabled)');
  await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 }).catch(() => {});
  await page.click('#addDevNext');
  await page.waitForSelector('#adResult', { timeout: 15000 });
  await page.click('#adResDone');
  await page.waitForSelector('#addDeviceModal', { state: 'hidden' });
}
// the model first, then Ethernet — or RS-485 — and where it is
async function addTo(port, reach) {
  await openAdd(page);
  await pickModel(page, TPL, TPL);
  await answer(page, 'PortKind', port);
  if (reach) await answer(page, 'Reach', reach);
}
const devOf = async id => ((await api('/api/devices')).body.devices || []).find(d => d.id === id) || {};

try {
  check('template saved (API)', tplSave.status === 200);
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ═══ 1. a slave behind a Modbus TCP gateway is a Modbus TCP device ══════
  await goDevices();
  await addTo('ethernet');
  const reaches = await page.$$eval('[name="csAddReach"]', els => els.map(e => e.value));
  check('Ethernet asks how it is reached: at an address, or through a Modbus TCP gateway',
    reaches.includes('address') && reaches.includes('bridge'), reaches.join(' | '));
  await answer(page, 'Reach', 'bridge');
  const gwOpts = await page.locator('#csAddBus option').allInnerTexts();
  check('…the gateway is listed', gwOpts.some(o => /E2E gateway.*192\.0\.2\.61:502/.test(o)), gwOpts.join(' | '));
  await page.selectOption('#csAddBus', `${BR.gw}::502`);
  await page.waitForTimeout(400);
  check('…through the gateway there is no host or port to type (they are the gateway\'s)',
    !(await page.isVisible('#csAddHost')) && !(await page.isVisible('#csAddPort')) && await page.isVisible('#csAddUnit'));
  await fill(page, 'Unit', 7);
  await finish(DEV.gw);
  let d = await devOf(DEV.gw);
  check('saved as Modbus TCP through the gateway, unit 7',
    d.protocol === 'tcp' && d.bridge === BR.gw && d.unit_id === 7 && d.host === '192.0.2.61', JSON.stringify(d).slice(0, 220));

  // the RS-485 bridges are those that carry RTU
  await addTo('serial', 'bridge');
  const rtOpts = await page.locator('#csAddBus option').allInnerTexts();
  check('RS-485 → through a bridge lists the transparent bridge, not the gateway',
    rtOpts.some(o => /E2E transparent/.test(o)) && !rtOpts.some(o => /E2E gateway/.test(o)), rtOpts.join(' | '));
  dlg.answers.push(true);
  await page.click('#addDevCancel');
  await page.waitForSelector('#addDeviceModal', { state: 'hidden' });

  // "+ Device" on the gateway's bus opens Modbus TCP, gateway preselected
  await goDevices();
  await page.locator(`.bridge-card[data-bridge="${BR.gw}"] [data-action="addDeviceOnBus"]`).click();
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForSelector('#csAddBus');
  await page.waitForTimeout(600);
  check('"+ Device" on a gateway\'s bus opens Ethernet through that gateway',
    await page.isChecked('#csAddPortKind-ethernet') && await page.inputValue('#csAddBus') === `${BR.gw}::502`);
  check('…and proposes the next free unit', await page.inputValue('#csAddUnit') === '1');
  dlg.answers.push(true);
  await page.click('#addDevCancel');
  await page.waitForSelector('#addDeviceModal', { state: 'hidden' });

  // ═══ 2. editing: Modbus variants switch, HTTP/MQTT stay out ═════════════
  check('a direct Modbus TCP device (API)', (await post('/api/devices', { id: DEV.tcp, template: TPL, enabled: false,
    connection: { protocol: 'tcp', host: '192.0.2.9', port: 502, unit_id: 1 } })).status === 200);
  await goDevices();
  // a saved device is edited in its Edit modal (⋯ → Edit connection…), in the
  // same connection form Add Device uses
  await page.evaluate(id => window.app.openDeviceEdit(id), DEV.tcp);
  // the connection step (L3.6): the port, then where the bus is, then the role
  await page.waitForSelector('#ddvConn input[name="ddvPortKind"]', { state: 'attached' });
  const offPort = async v => (await page.getAttribute(`input[name="ddvPortKind"][value="${v}"]`, 'aria-disabled')) === 'true';
  check('editing: RS-485 can be chosen, HTTP and MQTT cannot',
    !(await offPort('serial')) && await offPort('http') && await offPort('mqtt'));
  check('…and the reason says why', /fixed after creation|not reached over/i.test(await page.innerText('#ddvConn')));
  await page.click('#ddvConn label:has(#ddvPortKind-serial)');
  await page.click('#ddvConn label:has(#ddvReach-here)');
  await page.click('#ddvConn label:has(#ddvRole-poll)');
  check('…and says a change of how it is reached starts afresh', await page.isVisible('#ddvRouteHint'));
  await page.fill('#ddvSerial', '/dev/ttyE2EUX');
  await page.fill('#ddvUnit', '3');
  const nRevU = dlg.seen.length;
  const savedRtu = await saveDeviceEditor(page);      // the Edit modal (L3.4): a bus move, reviewed, answered yes
  check('…reviewed first (TCP → a serial port: a bus move), named', dlg.seen.length > nRevU && /Review/.test(dlg.seen[nRevU].title)
    && (dlg.seen[nRevU].details || []).some(x => /serial_port/.test(x)), JSON.stringify(dlg.seen[nRevU] || null));
  check('…saved from the Edit modal', savedRtu && savedRtu.ok());
  d = await devOf(DEV.tcp);
  check('…the device is now Modbus RTU on that port, same id and map',
    d.protocol === 'rtu' && d.serial?.serial_port === '/dev/ttyE2EUX' && d.template === TPL, JSON.stringify(d).slice(0, 220));

  // ═══ 2b. the wire variants: Modbus over UDP, Modbus ASCII ═══════════════
  await goDevices();
  await addTo('ethernet', 'address');
  await fill(page, 'Host', '192.0.2.44');
  await page.check('#csAddUdp');
  await page.waitForTimeout(300);
  await finish(DEV.udp);
  d = await devOf(DEV.udp);
  check('"Over UDP" saves a Modbus UDP device', d.protocol === 'udp' && d.host === '192.0.2.44', JSON.stringify(d).slice(0, 160));
  await addTo('serial', 'here');
  await answer(page, 'Speaks', 'modbus:ascii');
  await answer(page, 'Role', 'poll');
  await page.evaluate(() => { const a = document.getElementById('csAddSerialAdv'); if (a) a.open = true; });
  await page.fill('#csAddSerial', '/dev/ttyE2EASC');
  await page.locator('#csAddSerial').blur();
  await fill(page, 'Unit', 5);
  await finish(DEV.asc);
  d = await devOf(DEV.asc);
  check('Framing ASCII on a serial port saves a Modbus ASCII device', d.protocol === 'ascii' && d.serial?.serial_port === '/dev/ttyE2EASC',
    JSON.stringify(d).slice(0, 200));
  await goDevices();
  check('the device list names it Modbus ASCII', /ASCII/.test(await page.innerText('#devicesListView')));
  await page.evaluate(id => window.app.openDeviceEdit(id), DEV.asc);
  await page.waitForSelector('#ddvConn input[name="ddvSpeaks"]', { state: 'attached' });
  check('editing it shows a port here, Modbus ASCII',
    await page.isChecked('input[name="ddvReach"][value="here"]') && await page.isChecked('input[name="ddvSpeaks"][value="modbus:ascii"]'));
  await page.evaluate(id => window.app.openDeviceEdit(id), DEV.udp);
  await page.waitForSelector('#ddvConn input[name="ddvPortKind"]', { state: 'attached' });
  check('editing the UDP device shows Ethernet with "Over UDP" ticked',
    await page.isChecked('input[name="ddvPortKind"][value="ethernet"]') && await page.isChecked('#ddvUdp'));
  await page.evaluate(() => window.app.closeDeviceDetail(true));

  // ═══ 3. Virtual Meters: a disabled "Add instance" says why ══════════════
  await page.click('[data-page="vmeters"]');
  await page.waitForSelector('#vmAddInstanceBtn');
  const dis = await page.isDisabled('#vmAddInstanceBtn');
  const why = await page.getAttribute('#vmAddInstanceBtn', 'title');
  check('Virtual Meters: "Add instance" disabled carries the reason', !dis || (why && why.length > 10), `${dis} ${why}`);
  // a template is there but no port is left (QA-R2-05): the empty state must
  // not say "Use Add instance" next to a disabled button — it says what blocks it
  await page.route(/\/api\/virtual-meters(\?.*)?$/, r => r.fulfill({ json: {
    instances: [], port_range: { start: 1502, end: 1502, used: [1502], next_free: null } } }));
  await page.route(/\/api\/virtual-meters\/templates(\?.*)?$/, r => r.fulfill({ json: {
    templates: [{ id: 'e2e_ux_vm', name: 'E2E vmeter' }] } }));
  const noPort = async () => {
    await page.evaluate(() => window.app.renderVirtualMeters());
    await page.waitForSelector('#vmetersContent .empty-state');
    return { dis: await page.isDisabled('#vmAddInstanceBtn'), text: await page.innerText('#vmetersContent .empty-state') };
  };
  let np = await noPort();
  check('Virtual Meters, no free port: the button is disabled and the empty state says why, not "Use Add instance"',
    np.dis && /VMETER_PORT_END/.test(np.text) && !/Use “Add instance”/.test(np.text), np.text.replace(/\s+/g, ' '));
  await page.evaluate(() => window.app.setLanguage('ro', false));
  np = await noPort();
  check('ro: the same, in Romanian', /nu e disponibil/.test(np.text) && /VMETER_PORT_END/.test(np.text) && !/vmeter\./.test(np.text),
    np.text.replace(/\s+/g, ' '));
  await page.evaluate(() => window.app.setLanguage('en', false));
  await page.unrouteAll({ behavior: 'ignoreErrors' });

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await cleanup();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
