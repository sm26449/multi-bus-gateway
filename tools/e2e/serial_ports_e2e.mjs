// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The serial port picker of the connection form (ADR-0028), as a user meets
// it. GET /api/serial-ports is FAKED in the browser (page.route) with the Pi 5
// bench, so no hardware is needed: an FTDI adapter with a by-id link (free), a
// CH340 (free), HAT ch0 listened to by two Seplos packs, HAT ch1 polled by the
// Deye, a board UART not mapped into the container, one console hidden.
//
//   - Add Device → By connection → RS-485 → a port on this host → Modbus RTU
//     → Master (L3.9; the picker judges ports only once the role is said,
//     L3-F4): each port with its kind and state; the listened port is aria-disabled (focusable, not native
//     disabled) with its reason; the not-mapped one shows the --device flag
//     and the compose line; Refresh asks again and shows a new port; a port is
//     picked with the keyboard; the free-text path stays under "Advanced".
//   - Listen-only: the listened port may be joined, the polled one not.
//   - Save: the POST carries the FTDI's by-id link as serial_port; the device
//     page shows that port picked (its own claim is not its conflict), and a
//     pick there sends that port's path in the PUT.
//   - Romanian words, and a phone-width page with no sideways scroll.
//
// Against the safety fixture; it creates one device (sp-e2e) and deletes it.
//
//   tools/e2e/fixture_up.sh safety 18087
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 node tools/e2e/serial_ports_e2e.mjs
import { chromium } from 'playwright';
import { openDeviceTab, saveDeviceEditor, closeDeviceEditor, editorModalOpen } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';
import { openAdd, answer as ask, fill, next } from './lib/add-device.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const ID = 'sp-e2e';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + String(extra).slice(0, 300) : ''}`);
};
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};

const BY_ID = '/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B0045K08-if00-port0';
const port = (name, kind, extra = {}) => ({
  name, dev: `/dev/${name}`, path: `/dev/${name}`, stable_path: `/dev/${name}`, by_id: [], kind,
  vendor_id: '', product_id: '', serial: '', manufacturer: '', model: '', label: '', driver: '',
  rs485: null, state: 'free', reason: '', polled_by: [], listened_by: [], processes: [], mapped: true,
  fix: null, ...extra });
const PORTS = () => [
  port('ttyUSB0', 'usb', { model: 'USB Serial', vendor_id: '1a86', product_id: '7523' }),
  port('ttyUSB1', 'usb', { model: 'FT232R USB UART', serial: 'B0045K08', stable_path: BY_ID, by_id: [BY_ID] }),
  port('ttySC0', 'hat', { label: 'SC16IS752 ch0 (spi0.0)', state: 'listened', listened_by: ['seplos-adr1', 'seplos-adr2'],
    reason: 'listened to by seplos-adr1, seplos-adr2' }),
  port('ttySC1', 'hat', { label: 'SC16IS752 ch1 (spi0.0)', state: 'polled', polled_by: ['deye'], reason: 'polled by deye' }),
  port('ttyAMA0', 'board_uart', { label: 'PL011 (1f00030000.serial)', state: 'not_mapped', mapped: false,
    fix: { docker_run: '--device /dev/ttyAMA0:/dev/ttyAMA0', compose: '- /dev/ttyAMA0:/dev/ttyAMA0' } }),
];
let answer = { ports: PORTS(), hidden: 1 };
let asked = 0;

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }) {
  const ctx = await browser.newContext({ viewport, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.sent = [];
  page.on('request', r => { if (['POST', 'PUT', 'PATCH'].includes(r.method()) && /\/api\/devices/.test(r.url())) page.sent.push({ method: r.method(), url: r.url(), body: r.postData() }); });
  page.dlg = await watchDialogs(page);
  await page.route('**/api/serial-ports*', r => { asked++; r.fulfill({ json: answer }); });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  // the app is made on DOMContentLoaded and routes after its first fetches:
  // under load the phone page was asked for the wizard before it existed
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 20000 });
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}

async function directRtu(page) {
  await openAdd(page);
  await page.click('[data-ad-way="connection"]');
  await ask(page, 'PortKind', 'serial');
  await ask(page, 'Reach', 'here');
  await ask(page, 'Speaks', 'modbus:rtu');
  await ask(page, 'Role', 'poll');
  await page.waitForSelector('#csAddSerialList .cf-port');
}
const row = (page, name, px = 'csAdd') => page.locator(`#${px}SerialList .cf-port-row[data-port="${name}"]`);
const btn = (page, name, px = 'csAdd') => row(page, name, px).locator('button.cf-port');
async function why(page, name, px = 'csAdd') {
  const id = await btn(page, name, px).getAttribute('aria-describedby');
  return id ? (await page.locator(`#${id}`).innerText()).trim() : '';
}

try {
  for (const id of [ID, 'sp-e2e-fr']) await api(`/api/devices/${id}`, { method: 'DELETE' });   // leftovers of an aborted run
  const page = await open();
  await directRtu(page);

  // ---- 1. the list ---------------------------------------------------------
  const names = await page.locator('#csAddSerialList .cf-port-row').evaluateAll(els => els.map(e => e.dataset.port));
  check('every port is listed', names.join() === 'ttyUSB0,ttyUSB1,ttySC0,ttySC1,ttyAMA0', names.join());
  const usb1 = await btn(page, 'ttyUSB1').innerText();
  check('a USB port shows its name, model, kind and state', /ttyUSB1/.test(usb1) && /FT232R/.test(usb1) && /USB/.test(usb1) && /Free/i.test(usb1), usb1);
  check('the HAT channel shows its kind', /RS-485 HAT/i.test(await btn(page, 'ttySC1').innerText()));
  check('the board UART shows its kind', /Board UART/i.test(await btn(page, 'ttyAMA0').innerText()));
  check('the hidden console is said', /1 console port not shown/.test(await page.innerText('#csAddSerialList')));

  // ---- 2. a listened port may not be polled ------------------------------
  const sc0 = btn(page, 'ttySC0');
  check('the listened port is aria-disabled, not native disabled',
    (await sc0.getAttribute('aria-disabled')) === 'true' && !(await sc0.evaluate(e => e.disabled)));
  const sc0why = await why(page, 'ttySC0');
  check('its reason names who listens', /seplos-adr1, seplos-adr2/.test(sc0why) && /Listen only/.test(sc0why), sc0why);
  await sc0.click({ force: true });             // aria-disabled still answers a click
  check('clicking it picks nothing', (await page.inputValue('#csAddSerial')) === '');
  check('polling beside a poller is allowed, said', !(await btn(page, 'ttySC1').getAttribute('aria-disabled'))
    && /Polled by deye/.test(await why(page, 'ttySC1')));

  // ---- 3. not mapped: the lines to add -----------------------------------
  const amaWhy = await why(page, 'ttyAMA0');
  check('a port not mapped is aria-disabled', (await btn(page, 'ttyAMA0').getAttribute('aria-disabled')) === 'true');
  check('it shows the --device flag and the compose line',
    amaWhy.includes('--device /dev/ttyAMA0:/dev/ttyAMA0') && amaWhy.includes('- /dev/ttyAMA0:/dev/ttyAMA0'), amaWhy);

  // ---- 4. the path field stays under Advanced ------------------------------
  check('the path field is under a closed Advanced', !(await page.isVisible('#csAddSerial'))
    && (await page.getAttribute('#csAddSerialAdv', 'open')) === null);

  // ---- 5. keyboard: Tab from Refresh to the first port, Space picks it ------
  await page.focus('#csAddSerialRefresh');
  await page.keyboard.press('Tab');
  check('Tab reaches the first port', await page.evaluate(() => document.activeElement?.id) === 'csAddPort-0');
  await page.keyboard.press('Tab');
  check('Tab reaches the next port', await page.evaluate(() => document.activeElement?.id) === 'csAddPort-1');
  await page.keyboard.press('Space');
  check('Space picks it: its by-id link is the path', (await page.inputValue('#csAddSerial')) === BY_ID);
  check('it is shown picked, focus kept', (await btn(page, 'ttyUSB1').getAttribute('aria-pressed')) === 'true'
    && await page.evaluate(() => document.activeElement?.id) === 'csAddPort-1');
  await page.focus('#csAddPort-2');                       // the listened HAT channel
  await page.keyboard.press('Enter');
  check('Enter on a refused port changes nothing', (await page.inputValue('#csAddSerial')) === BY_ID);

  // ---- 6. Refresh ----------------------------------------------------------
  const before = asked;
  answer = { ports: [...PORTS(), port('ttyACM0', 'usb', { model: 'CDC modem' })], hidden: 1 };
  await page.click('#csAddSerialRefresh');
  await page.waitForSelector('#csAddSerialList .cf-port-row[data-port="ttyACM0"]');
  check('Refresh asks the gateway again', asked > before);
  check('Refresh says how many it found', /6 serial ports found/.test(await page.innerText('#csAddSerialStatus')));
  check('the pick survives a Refresh', (await btn(page, 'ttyUSB1').getAttribute('aria-pressed')) === 'true');
  answer = { ports: PORTS(), hidden: 1 };

  // ---- 7. Listen-only: who may join ----------------------------------------
  await ask(page, 'Role', 'listen');
  await page.waitForSelector('#csAddSerialList .cf-port');
  check('a tap may join a listened port', !(await btn(page, 'ttySC0').getAttribute('aria-disabled')));
  check('a tap may not listen on a polled port', (await btn(page, 'ttySC1').getAttribute('aria-disabled')) === 'true'
    && /never both/.test(await why(page, 'ttySC1')));
  check('the pick survives a change of role', (await page.inputValue('#csAddSerial')) === BY_ID);
  await ask(page, 'Role', 'poll');
  await page.waitForSelector('#csAddSerialList .cf-port');

  // ---- 8. Save: the stored path is the stable one -------------------------
  await fill(page, 'Unit', 7);
  await next(page);                                    // → the model (By connection)
  await page.click('#adResults [data-ad-tpl="eastron_sdm120"]');
  await next(page);                                    // → Check (nothing on this host answers)
  await page.waitForFunction(() => document.querySelector('#adCheck .pv-refused, #adCheck .pv-status:not(.is-running)'), null, { timeout: 25000 }).catch(() => {});
  await next(page);                                    // → Name & save, without a check
  await page.fill('#adId', ID);
  await page.locator('#adId').dispatchEvent('input');
  if (await page.isChecked('#adEnabled')) await page.click('label:has(#adEnabled)');   // never polled
  await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 }).catch(() => {});
  await page.click('#addDevNext');
  await page.waitForSelector('#adResult', { timeout: 15000 }).catch(() => {});
  await page.click('#adResDone').catch(() => {});
  const post = page.sent.find(r => r.method === 'POST' && r.url.endsWith('/api/devices'));
  const body = post ? JSON.parse(post.body || '{}') : {};
  check('the POST carries the by-id link as serial_port', body.connection?.serial_port === BY_ID, post?.body);
  const dev = ((await api('/api/devices')).body.devices || []).find(d => d.id === ID);
  check('the gateway stored it', dev?.serial?.serial_port === BY_ID || dev?.connection?.serial_port === BY_ID, JSON.stringify(dev?.serial || dev?.connection));

  // ---- 9. the device page: its own port picked, another pick sent -----------
  answer = { ports: PORTS().map(p => p.name === 'ttyUSB1' ? { ...p, state: 'polled', polled_by: [ID] } : p), hidden: 1 };
  await page.evaluate(() => { window.app._serialPorts = null; });
  await page.evaluate(id => window.app.openDeviceDetail(id), ID);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvSerialList .cf-port');
  check('device page: its port is shown picked', (await btn(page, 'ttyUSB1', 'ddv').getAttribute('aria-pressed')) === 'true');
  check('device page: its own claim is not its conflict', !(await btn(page, 'ttyUSB1', 'ddv').getAttribute('aria-disabled'))
    && /Free/i.test(await btn(page, 'ttyUSB1', 'ddv').innerText()));
  check('device page: the listened port is refused there too', (await btn(page, 'ttySC0', 'ddv').getAttribute('aria-disabled')) === 'true');
  const from = page.sent.length;
  await btn(page, 'ttyUSB0', 'ddv').click();
  const nRevS = page.dlg.seen.length;
  await saveDeviceEditor(page);                       // the Edit modal (a bus move: reviewed, answered yes)
  check('device page: another port is reviewed first (a bus move), named', page.dlg.seen.length > nRevS && /Review/.test(page.dlg.seen[nRevS].title)
    && (page.dlg.seen[nRevS].details || []).some(x => /serial_port/.test(x)), JSON.stringify(page.dlg.seen[nRevS] || null));
  await page.waitForTimeout(600);
  const put = page.sent.slice(from).find(r => r.method === 'PATCH' && r.url.endsWith(`/api/devices/${ID}`));
  const pb = put ? (JSON.parse(put.body || '{}').sections || {}) : {};
  check('device page: the Edit modal\'s PATCH sends the picked path', pb.connection?.serial_port === '/dev/ttyUSB0', put?.body);
  // ---- 9b. production: the adapter is in the container only as an alias ----
  // (review B1) sysfs says ttyUSB0, compose maps it as /dev/ttyFRONIUS only
  const FR = 'sp-e2e-fr';
  const frBody = { ...body, id: FR, name: 'Fronius alias', connection: { ...body.connection, serial_port: '/dev/ttyFRONIUS' } };
  const made = await api('/api/devices', { method: 'POST', body: JSON.stringify(frBody) });
  check('the alias device is created', made.status < 300, JSON.stringify(made.body).slice(0, 200));
  answer = { ports: PORTS().map(p => p.name !== 'ttyUSB0' ? p : { ...p, mapped: true, aliases: ['/dev/ttyFRONIUS'],
    stable_path: '/dev/ttyFRONIUS', state: 'polled', base_state: 'free', polled_by: [FR] })
    // a host port whose name the container gave another device (a renamed mapping)
    .concat([port('ttyFRONIUS', 'usb', { stable_path: '', path_taken: true, mapped: false, state: 'not_mapped',
      base_state: 'not_mapped', fix: { docker_run: '--device /dev/ttyFRONIUS:/dev/ttyFRONIUS-x',
        compose: '- /dev/ttyFRONIUS:/dev/ttyFRONIUS-x', note: 'another device' } })]), hidden: 1 };
  await page.evaluate(() => { window.app._serialPorts = null; });
  await page.evaluate(id => window.app.openDeviceDetail(id), FR);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvSerialList .cf-port');
  const frBtn = btn(page, 'ttyUSB0', 'ddv');
  check('alias: its row is shown current', (await frBtn.getAttribute('aria-pressed')) === 'true');
  check('alias: a host port whose name is taken here is not lit too',
    (await page.locator('#ddvSerialList button.cf-port[aria-pressed="true"]').count()) === 1);
  check('alias: the row shows the alias, never /dev/ttyUSB0',
    /\/dev\/ttyFRONIUS/.test(await frBtn.innerText()) && !/\/dev\/ttyUSB0/.test(await frBtn.innerText()));
  check('alias: the path field keeps the alias, Advanced closed',
    (await page.inputValue('#ddvSerial')) === '/dev/ttyFRONIUS' && (await page.getAttribute('#ddvSerialAdv', 'open')) === null);
  await frBtn.click();
  check('alias: picking its row again keeps the alias', (await page.inputValue('#ddvSerial')) === '/dev/ttyFRONIUS');
  await api(`/api/devices/${FR}`, { method: 'DELETE' });
  check('no page errors (en)', page.errs.length === 0, page.errs.join(' | '));

  // ---- 10. Romanian -------------------------------------------------------
  answer = { ports: PORTS(), hidden: 1 };
  const ro = await open();
  await ro.evaluate(() => window.app.setLanguage('ro', false));
  await ro.waitForTimeout(300);
  await directRtu(ro);
  const roText = await ro.innerText('#csAddSerialPick');
  check('ro: kinds and states in Romanian', /Ascultat/i.test(roText) && /Interogat/i.test(roText) && /Nemapat/i.test(roText)
    && /UART al plăcii/i.test(roText) && /Liber/i.test(roText), roText);
  check('ro: the reason in Romanian', /Ascultat de seplos-adr1/.test(await why(ro, 'ttySC0')));
  check('ro: Refresh and Advanced in Romanian', /Reîmprospătează/.test(roText) && /Avansat/.test(roText));
  check('no page errors (ro)', ro.errs.length === 0, ro.errs.join(' | '));

  // ---- 11. a phone ---------------------------------------------------------
  const phone = await open({ width: 375, height: 800 });
  await directRtu(phone);
  const sideways = await phone.evaluate(() => document.scrollingElement.scrollWidth > window.innerWidth + 1);
  check('phone width: no sideways scroll', !sideways);
} catch (e) {
  check('no exception', false, e.stack || String(e));
} finally {
  await api(`/api/devices/${ID}`, { method: 'DELETE' }).catch(() => {});
  await api('/api/devices/sp-e2e-fr', { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
