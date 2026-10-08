// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// New-user friction fixed in 3.91.0, as a user meets it:
//  - a device behind a Modbus TCP gateway is added as Modbus TCP ("Reached:
//    through the gateway"), not under RTU; the RTU picker lists only RTU bridges;
//  - editing a Modbus device can switch among the Modbus variants;
//  - a disabled "Add instance" (Virtual Meters) says why.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node ux_gaps_e2e.mjs
import { chromium } from 'playwright';

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
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
async function goDevices() {
  await page.click('[data-page="devices"]');
  if (!(await page.isVisible('#devicesListView')))
    await page.click('#deviceRegistersView [data-action="closeDeviceRegisters"]');
  await page.waitForSelector('#devicesListView', { state: 'visible' });
}
async function finish(id) {
  await page.click('#devWizNext');
  await page.click(`.tpl-pick[data-tpl="${TPL}"]`);
  await page.click('#devWizNext');
  await page.fill('#devWizId', id);
  await page.fill('#devWizName', id);
  if (await page.isChecked('#devWizEnabled')) await page.click('label:has(#devWizEnabled)');
  await page.click('#devWizNext');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });
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
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.waitForSelector('#devWizGw');
  const gwOpts = await page.locator('#devWizGw option').allInnerTexts();
  check('Modbus TCP offers "Reached: directly, or through the gateway"',
    /Directly/.test(gwOpts[0]) && gwOpts.some(o => /E2E gateway.*192\.0\.2\.61:502/.test(o)), gwOpts.join(' | '));
  await page.selectOption('#devWizGw', `${BR.gw}::502`);
  await page.waitForSelector('#devWizHost', { state: 'hidden' });
  check('…choosing the gateway hides host and port (they are the gateway\'s)',
    !(await page.isVisible('#devWizPort')) && await page.isVisible('#devWizUnit'));
  await page.fill('#devWizUnit', '7');
  await finish(DEV.gw);
  let d = await devOf(DEV.gw);
  check('saved as Modbus TCP through the gateway, unit 7',
    d.protocol === 'tcp' && d.bridge === BR.gw && d.unit_id === 7 && d.host === '192.0.2.61', JSON.stringify(d).slice(0, 220));

  // the RTU picker lists only bridges that carry RTU
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  await page.waitForSelector('#devWizBridge');
  const rtOpts = await page.locator('#devWizBridge option').allInnerTexts();
  check('Modbus RTU → Over network lists the transparent bridge, not the gateway',
    rtOpts.some(o => /E2E transparent/.test(o)) && !rtOpts.some(o => /E2E gateway/.test(o)), rtOpts.join(' | '));
  await page.click('#deviceWizardModal [data-action="closeModal"]');

  // "+ Device" on the gateway's bus opens Modbus TCP, gateway preselected
  await goDevices();
  await page.locator(`.bridge-card[data-bridge="${BR.gw}"] [data-action="addDeviceOnBus"]`).click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.waitForSelector('#devWizGw');
  check('"+ Device" on a gateway\'s bus opens Modbus TCP with that gateway',
    await page.isChecked('input[name="devWizProto"][value="tcp"]') && await page.inputValue('#devWizGw') === `${BR.gw}::502`);
  check('…and proposes the next free unit', await page.inputValue('#devWizUnit') === '1');
  await page.click('#deviceWizardModal [data-action="closeModal"]');

  // ═══ 2. editing: Modbus variants switch, HTTP/MQTT stay out ═════════════
  check('a direct Modbus TCP device (API)', (await post('/api/devices', { id: DEV.tcp, template: TPL, enabled: false,
    connection: { protocol: 'tcp', host: '192.0.2.9', port: 502, unit_id: 1 } })).status === 200);
  await goDevices();
  await page.evaluate(id => window.app.openDeviceWizard(id), DEV.tcp);
  await page.waitForSelector('#deviceWizardModal.active');
  check('editing: Modbus RTU can be chosen, HTTP and MQTT cannot',
    !(await page.isDisabled('input[name="devWizProto"][value="rtu"]'))
    && await page.isDisabled('input[name="devWizProto"][value="http"]')
    && await page.isDisabled('input[name="devWizProto"][value="mqtt"]'));
  check('…and the hint says why', /register map stays valid/.test(await page.innerText('#devWizBody')));
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  await page.click('#deviceWizardModal label:has(input[name="devWizRtuMode"][value="rtu"])');
  await page.fill('#devWizSerial', '/dev/ttyE2EUX');
  await page.fill('#devWizUnitR', '3');
  await page.click('#devWizNext');
  await page.click('#devWizNext');
  await page.click('#devWizNext');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });
  d = await devOf(DEV.tcp);
  check('…the device is now Modbus RTU on that port, same id and map',
    d.protocol === 'rtu' && d.serial?.serial_port === '/dev/ttyE2EUX' && d.template === TPL, JSON.stringify(d).slice(0, 220));

  // ═══ 2b. the wire variants: Modbus over UDP, Modbus ASCII ═══════════════
  await goDevices();
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.selectOption('#devWizGw', '');
  await page.fill('#devWizHost', '192.0.2.44');
  await page.check('#devWizUdp');
  await finish(DEV.udp);
  d = await devOf(DEV.udp);
  check('"Over UDP" saves a Modbus UDP device', d.protocol === 'udp' && d.host === '192.0.2.44', JSON.stringify(d).slice(0, 160));
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal.active');
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  await page.click('#deviceWizardModal label:has(input[name="devWizRtuMode"][value="rtu"])');
  await page.fill('#devWizSerial', '/dev/ttyE2EASC');
  await page.selectOption('#devWizFraming', 'ascii');
  await page.fill('#devWizUnitR', '5');
  await finish(DEV.asc);
  d = await devOf(DEV.asc);
  check('Framing ASCII on a serial port saves a Modbus ASCII device', d.protocol === 'ascii' && d.serial?.serial_port === '/dev/ttyE2EASC',
    JSON.stringify(d).slice(0, 200));
  await goDevices();
  check('the device list names it Modbus ASCII', /ASCII/.test(await page.innerText('#devicesListView')));
  await page.evaluate(id => window.app.openDeviceWizard(id), DEV.asc);
  await page.waitForSelector('#deviceWizardModal.active');
  check('editing it shows Direct serial with ASCII framing',
    await page.isChecked('input[name="devWizRtuMode"][value="rtu"]') && await page.inputValue('#devWizFraming') === 'ascii');
  await page.click('#deviceWizardModal [data-action="closeModal"]');
  await page.evaluate(id => window.app.openDeviceWizard(id), DEV.udp);
  await page.waitForSelector('#deviceWizardModal.active');
  check('editing the UDP device shows Modbus TCP with "Over UDP" ticked',
    await page.isChecked('input[name="devWizProto"][value="tcp"]') && await page.isChecked('#devWizUdp'));
  await page.click('#deviceWizardModal [data-action="closeModal"]');

  // ═══ 3. Virtual Meters: a disabled "Add instance" says why ══════════════
  await page.click('[data-page="vmeters"]');
  await page.waitForSelector('#vmAddInstanceBtn');
  const dis = await page.isDisabled('#vmAddInstanceBtn');
  const why = await page.getAttribute('#vmAddInstanceBtn', 'title');
  check('Virtual Meters: "Add instance" disabled carries the reason', !dis || (why && why.length > 10), `${dis} ${why}`);

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
