// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The connection step (ui/js/conn-step.js, ADR-0031 §2/§4, L3.6) in the Edit
// device modal's Connection section, on every way a device is reached:
//
// A  RTU on a serial port here: port → reach → protocol → role → line →
//    address, each answered as stored; HTTP/MQTT/CAN aria-disabled with the
//    class lock's words; no "recommended" default (L3-F5)
// B  RTU through a bridge: the bus picked; the line read-only, set on the box
// C  a tap through a bridge, D a tap here: the role Listen-only
// E  Seplos V2 listening: the protocol a fact, its role as stored; a bus
//    change asks the role again (M7)
// F  Modbus TCP: Master, Listen-only refused with the server's words
// G  CAN: the listen-only gate with the exact ip link command
// H  HTTP: host + device id with the template's path; an id edited is saved
// I  MQTT: the broker and the topic
// J  role lock texts: a second poller on the bus refuses Listen-only, names it;
//    the line inherited, locked, its reason with Edit bus line…
// K  Edit bus line on a two-device tty: the dry run lists both, they change
//    and restart; the modal reads the device again, the edits kept
// L  keyboard: an aria-disabled role is focusable, never taken; Romanian
//
// Against a throwaway gateway of the qa fixture, login on:
//   MBG_FX_NAME=mbg-fx-cs tools/e2e/fixture_up.sh qa 18341 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18341 MBG_PASS=e2e-pass node tools/e2e/conn_step_e2e.mjs
//   docker rm -f mbg-fx-cs
// It creates cs-* devices (disabled, except a second poller on /dev/ttyQA1,
// a path that does not exist) and deletes them; rtu1's line is put back.
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18341';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)).slice(0, 400) : ''}`);
};

const api = await pwRequest.newContext({ baseURL: BASE });
await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const dev = async id => (await (await api.get(`/api/devices/${id}`)).json()).device;
const mk = async body => {
  await api.delete(`/api/devices/${body.id}`);
  const r = await api.post('/api/devices', { data: body });
  check(`(made ${body.id})`, r.ok(), await r.text());
};
const CREATED = ['cs-sv', 'cs-can', 'cs-http', 'cs-mqtt', 'cs-rtu2'];
const rtu1Line = (await dev('rtu1')).connection;

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|412|409|422/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.sent = [];
  page.on('request', r => { if (r.method() !== 'GET') page.sent.push({ method: r.method(), url: r.url(), body: r.postData() }); });
  if (opts.lang) await page.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, opts.lang);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
async function conn(page, id) {
  if (await page.locator('#devEditModal.active').count()) await cancel(page);
  await page.evaluate(h => { location.hash = h; }, `#/devices/${id}`);
  await page.waitForSelector('#deviceDetailView #ddvFacts', { state: 'visible' });
  await page.waitForFunction(i => window.app?._devDetail?.id === i, id);
  await page.waitForTimeout(300);
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.click('#ddvActionsMenu [data-ddv-act="connection"]');
  await page.waitForSelector('#devEditModal.active #ddvCsHost');
  // the buses and the matrix are read: the questions drawn from them
  await page.waitForFunction(() => !document.querySelector('#ddvCsHost .cs-loading'));
  await page.waitForTimeout(200);
}
async function cancel(page) {
  page.dlg.answers.push(true);                       // a dirty dialog: discard
  await page.click('#devEditCancel');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
  page.dlg.answers.length = 0;
}
async function settled(page) {
  await page.waitForTimeout(450);
  await page.waitForFunction(() => !/Checking|Se verifică/.test(document.getElementById('devEditSummary')?.textContent || ''));
}
const checked = (page, name) => page.evaluate(n => document.querySelector(`#ddvCsHost input[name="${n}"]:checked`)?.value ?? null, name);
const offWhy = (page, id) => page.evaluate(i => {
  const el = document.getElementById(i);
  if (!el) return null;
  const why = document.getElementById(el.getAttribute('aria-describedby') || '');
  return { off: el.getAttribute('aria-disabled') === 'true', disabled: el.disabled, focusable: el.tabIndex >= 0, why: why ? why.textContent : '' };
}, id);
const text = page => page.locator('#ddvConn').innerText();

try {
  await mk({ id: 'cs-sv', name: 'CS Seplos', template: 'seplos_bms_v2', enabled: false,
    connection: { protocol: 'seplos_v2', serial_port: '/dev/ttyQA5', mode: 'listen', unit_id: 1, baudrate: 9600 } });
  await mk({ id: 'cs-can', name: 'CS CAN', template: 'pylontech_can_inverter', enabled: false,
    connection: { protocol: 'can', channel: 'vcan9', bitrate: 500000 } });
  await mk({ id: 'cs-http', name: 'CS Solar API', template: 'fronius_solar_api_meter', enabled: false,
    connection: { protocol: 'http', url: 'http://192.168.250.5/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=1' } });
  await mk({ id: 'cs-mqtt', name: 'CS MQTT', template: 'mqtt_json_generic', enabled: false,
    connection: { protocol: 'mqtt', broker: '127.0.0.1', port: 1883, topic: 'cs/e2e/#' } });
  const page = await open();

  // ── A. RTU on a serial port here ─────────────────────────────────────
  await conn(page, 'rtu1');
  check('A rtu1: port RS-485, reach here, Modbus RTU, Master — as stored',
        (await checked(page, 'ddvPortKind')) === 'serial' && (await checked(page, 'ddvReach')) === 'here'
        && (await checked(page, 'ddvSpeaks')) === 'modbus:rtu' && (await checked(page, 'ddvRole')) === 'poll',
        [await checked(page, 'ddvPortKind'), await checked(page, 'ddvReach'), await checked(page, 'ddvSpeaks'), await checked(page, 'ddvRole')]);
  const httpOff = await offWhy(page, 'ddvPortKind-http');
  check('A HTTP: shown, aria-disabled, focusable, never native disabled, with its reason (the model, or the class lock)', httpOff && httpOff.off
        && !httpOff.disabled && httpOff.focusable && /not reached over Web API|fixed after creation/i.test(httpOff.why), httpOff);
  check('A the line its own (alone on its bus), the unit as stored', (await page.inputValue('#ddvUnit')) === '3'
        && /its own/i.test(await page.locator('#ddvLineWhy').innerText()) && (await page.getAttribute('#ddvBaud', 'aria-disabled')) !== 'true');
  check('A no "recommended" default anywhere (L3-F5)', !/recommended/i.test(await text(page)));
  check('A the stored values with their sources, folded', (await page.locator('#ddvCsAdv summary').count()) === 1
        && (await page.getAttribute('#ddvCsAdv', 'open')) === null);
  await settled(page);
  check('A untouched: nothing to save', /No changes/.test(await page.locator('#devEditSummary').innerText()));
  await cancel(page);

  // ── B. RTU through a bridge ──────────────────────────────────────────
  await conn(page, 'brpoll');
  check('B brpoll: through a bridge, its bus picked', (await checked(page, 'ddvReach')) === 'bridge'
        && (await page.inputValue('#ddvBus')) === 'panel::14197', await page.inputValue('#ddvBus').catch(() => ''));
  check('B the line is the box\'s (a transparent converter): read-only, set on the box',
        /set on the box/i.test(await page.locator('#ddvLineQ').innerText()) && !(await page.locator('#ddvBaud').count()));
  check('B Master; its unit', (await checked(page, 'ddvRole')) === 'poll' && (await page.inputValue('#ddvUnit')) === '4');
  await cancel(page);

  // ── C, D. taps ───────────────────────────────────────────────────────
  await conn(page, 'brtap');
  check('C brtap: Listen-only through the bridge', (await checked(page, 'ddvRole')) === 'listen' && (await checked(page, 'ddvReach')) === 'bridge');
  await cancel(page);
  await conn(page, 'tap1');
  check('D tap1: Listen-only on a port here', (await checked(page, 'ddvRole')) === 'listen' && (await checked(page, 'ddvReach')) === 'here'
        && (await page.inputValue('#ddvSerial')) === '/dev/ttyQA0');
  check('D no timeout asked of a listener', !(await page.locator('#ddvTimeout').count()));
  await cancel(page);

  // ── E. Seplos V2 listening ───────────────────────────────────────────
  await conn(page, 'cs-sv');
  check('E Seplos: the protocol, its role as stored (Listen-only), a pack address',
        (await checked(page, 'ddvSpeaks')) === 'seplos_v2' && (await checked(page, 'ddvRole')) === 'listen'
        && /Pack address/.test(await page.locator('#ddvAddrQ').innerText()));
  await page.locator('#ddvSerialAdv').evaluate(d => { d.open = true; });
  await page.fill('#ddvSerial', '/dev/ttyQA6');
  await page.locator('#ddvSerial').dispatchEvent('change');
  await page.waitForTimeout(300);
  check('E another bus: the Seplos role is asked again, none chosen (M7)', (await checked(page, 'ddvRole')) === null,
        await checked(page, 'ddvRole'));
  await settled(page);
  check('E …and Save waits for it, the question marked', /ask the packs, or only listen/.test(await page.locator('#devEditErrors').innerText().catch(() => ''))
        && (await page.getAttribute('#ddvRole-poll', 'aria-invalid')) === 'true', await page.locator('#devEditErrors').innerText().catch(() => ''));
  await cancel(page);

  // ── F. Modbus TCP ────────────────────────────────────────────────────
  await conn(page, 'em1');
  check('F em1: Ethernet, directly, Master', (await checked(page, 'ddvPortKind')) === 'ethernet'
        && (await checked(page, 'ddvReach')) === 'address' && (await checked(page, 'ddvRole')) === 'poll'
        && (await page.inputValue('#ddvHost')) === '127.0.0.1');
  const loF = await offWhy(page, 'ddvRole-listen');
  check('F Listen-only refused with the server\'s words (a Modbus TCP server only answers)', loF && loF.off && /only answers the questions/.test(loF.why), loF);
  await cancel(page);

  // ── G. CAN ───────────────────────────────────────────────────────────
  await conn(page, 'cs-can');
  const gate = await page.locator('#ddvCanGate').innerText().catch(() => '');
  check('G CAN: the gate names the interface and gives the exact command', /vcan9/.test(gate)
        && /ip link set vcan9 down && ip link set vcan9 type can bitrate 500000 listen-only on && ip link set vcan9 up/.test(gate), gate);
  check('G the role is Listen-only, said', /Listen-only/.test(await page.locator('#ddvCanFields').innerText()));
  await page.fill('#ddvCanChannel', 'vcan8');
  await page.locator('#ddvCanChannel').dispatchEvent('change');
  await settled(page);
  check('G a changed interface the host has not: a hard gate (Save refused, said at the field)',
        (await page.getAttribute('#ddvCanChannel', 'aria-invalid')) === 'true' || /vcan8/.test(await page.locator('#devEditErrors').innerText().catch(() => '')));
  await cancel(page);

  // ── H. HTTP: host + device id ────────────────────────────────────────
  await conn(page, 'cs-http');
  check('H HTTP: the host and the device id, read from the stored URL', (await page.inputValue('#ddvHttpHost')) === "192.168.250.5"
        && (await page.inputValue('#ddvHttpId')) === '1');
  check('H the template\'s call is said', /GetMeterRealtimeData/.test(await page.locator('#ddvHttpFields').innerText()));
  await page.fill('#ddvHttpId', '2');
  await settled(page);
  const n = page.sent.length;
  page.dlg.answers.push(true);
  await page.click('#devEditSave');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'), null, { timeout: 8000 }).catch(() => {});
  const putH = page.sent.slice(n).find(r => r.method === 'PATCH' && !/dry_run/.test(r.url));
  check('H …saved as the URL with that id', /DeviceId=2/.test(putH?.body || '') && /DeviceId=2/.test((await dev('cs-http')).connection.url || ''), putH?.body);

  // ── I. MQTT ──────────────────────────────────────────────────────────
  await conn(page, 'cs-mqtt');
  check('I MQTT: its broker and topic', (await page.inputValue('#ddvBroker')) === '127.0.0.1' && (await page.locator('#ddvMqttTopic').count()) === 1);
  check('I no role, no line: a broker is not a bus', !(await page.locator('#ddvRoleQ').count()) && !(await page.locator('#ddvLineQ').count()));
  await cancel(page);

  // ── J. a second poller on rtu1's bus: role and line locks ────────────
  await mk({ id: 'cs-rtu2', name: 'CS second poller', template: 'eastron_sdm120', enabled: true,
    connection: { protocol: 'rtu', serial_port: '/dev/ttyQA1', baudrate: 9600, parity: 'N', stopbits: 1, bytesize: 8, unit_id: 5, timeout: 1 } });
  await conn(page, 'rtu1');
  const loJ = await offWhy(page, 'ddvRole-listen');
  check('J Listen-only refused: polled by the other device, named', loJ && loJ.off && /CS second poller/.test(loJ.why), loJ);
  check('J the server\'s lock list says it too', /connection\.role/.test(await page.locator('#devEditPanel-connection .dev-shell-locks').innerText()));
  const baud = await offWhy(page, 'ddvBaud');
  check('J the line inherited from the bus: locked, focusable, its reason with who reads it', baud && baud.off && baud.focusable
        && /CS second poller|cs-rtu2/.test(await page.locator('#ddvLineWhy').innerText())
        && /9600 8N1/.test(await page.locator('#ddvLineWhy').innerText()), [baud, await page.locator('#ddvLineWhy').innerText()]);
  check('J …and Edit bus line… beside it', await page.locator('#ddvEditBusLine').isVisible());

  // ── K. Edit bus line on the two-device tty ───────────────────────────
  const nK = page.dlg.seen.length;
  page.dlg.answers.push({ baudrate: '19200', parity: 'E', bytesize: '8', stopbits: '1' }, true);
  await page.click('#ddvEditBusLine');
  await page.waitForFunction(n0 => true, nK);
  await page.waitForTimeout(2500);
  const ask = page.dlg.seen[nK], review = page.dlg.seen[nK + 1];
  check('K asked in the page\'s dialog: the line, four fields', ask && (ask.fields || []).length === 4, ask);
  check('K the review lists both devices that change and restart', review && review.kind === 'confirm'
        && (review.details || []).some(x => /rtu1/.test(x)) && (review.details || []).some(x => /cs-rtu2/.test(x))
        && /19200 8E1/.test(review.title || ''), review);
  const r1 = await dev('rtu1'), r2 = await dev('cs-rtu2');
  check('K both now read the bus at 19200 8E1', r1.connection.baudrate === 19200 && r1.connection.parity === 'E'
        && r2.connection.baudrate === 19200 && r2.connection.parity === 'E', [r1.connection, r2.connection]);
  const bl = page.sent.filter(r => r.method === 'PATCH' && /\/api\/buses\//.test(r.url));
  check('K one dry run, then one change', bl.length === 2 && /dry_run=true/.test(bl[0].url) && !/dry_run/.test(bl[1].url), bl.map(r => r.url));
  check('K the modal read the device again: its line now 19200, said', (await page.inputValue('#ddvBaud')) === '19200'
        && /bus line was changed/i.test(await page.locator('#devEditModal').innerText()));
  await cancel(page);

  // ── L. keyboard and Romanian ─────────────────────────────────────────
  await conn(page, 'rtu1');
  await page.focus('#ddvRole-poll');
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(300);
  check('L an arrow onto a refused role leaves the role as it was', (await checked(page, 'ddvRole')) === 'poll');
  await cancel(page);
  // M3: the arrows move the focus through every option, a refused one too
  // (its reason said), and choose nothing; Space / Enter chooses
  await conn(page, 'rtu1');
  const focused = () => page.evaluate(() => document.activeElement?.id);
  const live = () => page.evaluate(() => document.getElementById('ddvCsLive')?.textContent || '');
  await page.focus('#ddvPortKind-serial');
  await page.keyboard.press('ArrowRight');
  check('M3 → Ethernet: focused, not chosen', (await focused()) === 'ddvPortKind-ethernet' && (await checked(page, 'ddvPortKind')) === 'serial');
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(150);
  check('M3 → CAN (refused, between two others): focused, its reason said, not chosen', (await focused()) === 'ddvPortKind-can'
        && /CAN/.test(await live()) && (await checked(page, 'ddvPortKind')) === 'serial', await live());
  await page.keyboard.press('ArrowRight');
  check('M3 → past it to the next', (await focused()) === 'ddvPortKind-http');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(150);
  check('M3 Enter on a refused one: nothing chosen, why said', (await checked(page, 'ddvPortKind')) === 'serial' && /HTTP/.test(await live()));
  await page.keyboard.press('Home');
  check('M3 Home: the first option', (await focused()) === 'ddvPortKind-serial');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press(' ');
  await page.waitForTimeout(300);
  check('M3 Space chooses the focused option', (await checked(page, 'ddvPortKind')) === 'ethernet', await checked(page, 'ddvPortKind'));
  await cancel(page);
  const ro = await open({ lang: 'ro' });
  await conn(ro, 'rtu1');
  check('L ro: the questions in Romanian', /Rolul pe magistrală/i.test(await ro.locator('#ddvConn').innerText())
        && /Unde e magistrala/i.test(await ro.locator('#ddvConn').innerText()), (await ro.locator('#ddvConn').innerText()).slice(0, 300));
  await cancel(ro);
  const phone = await open({ viewport: { width: 390, height: 844 } });
  await conn(phone, 'rtu1');
  const wide = await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  check('L a phone: no sideways scroll', !wide);
  await cancel(phone);

  // ── M. the Add step API (what the steps-mode shell mounts) ───────────
  const tpls = (await (await api.get('/api/device-templates')).json()).templates || [];
  const sdm = tpls.find(x => x.id === 'eastron_sdm120'), sv = tpls.find(x => x.id === 'seplos_bms_v2');
  await page.evaluate(t => {
    document.getElementById('csAddHost')?.remove();
    document.body.insertAdjacentHTML('beforeend', '<div id="csAddHost" style="position:fixed;inset:0;overflow:auto;background:var(--bg-primary);z-index:3000;padding:16px"></div>');
    window.__cs = window.mountConnStep(document.getElementById('csAddHost'), { context: 'add', template: t });
  }, sdm);
  await page.waitForFunction(() => !document.querySelector('#csAddHost .cs-loading'));
  const addErr = () => page.evaluate(() => Object.keys(window.__cs.errors()));
  check('M add: no port chosen for a model with two, the question asked', (await page.evaluate(() => document.querySelector('input[name="csAddPortKind"]:checked'))) === null
        && (await addErr()).includes('connection.port_kind'), await addErr());
  await page.click('label:has(#csAddPortKind-serial)');
  check('M RS-485: where the bus is is asked, nothing chosen for it (L3-F5)', (await page.evaluate(() => document.querySelector('input[name="csAddReach"]:checked'))) === null
        && (await addErr()).includes('connection.reach'), await addErr());
  await page.click('label:has(#csAddReach-here)');
  await page.locator('#csAddSerialAdv').evaluate(d => { d.open = true; });
  await page.fill('#csAddSerial', '/dev/ttyQA1');
  await page.locator('#csAddSerial').dispatchEvent('change');
  await page.waitForTimeout(400);
  check('M a polled bus: Master pre-filled, with why', (await page.evaluate(() => document.querySelector('input[name="csAddRole"]:checked')?.value)) === 'poll'
        && /Pre-filled/.test(await page.locator('#csAddRoleNote').innerText()), await page.locator('#csAddHost').innerText());
  check('M …the line inherited (19200 8E1 since K), locked', (await page.inputValue('#csAddBaud')) === '19200' && (await page.inputValue('#csAddParity')) === 'E'
        && (await page.getAttribute('#csAddBaud', 'aria-disabled')) === 'true');
  check('M …the next free address, said', (await page.inputValue('#csAddUnit')) === '1' && /3 is rtu1/.test(await page.locator('#csAddAddrHint').innerText())
        && /1 is free/.test(await page.locator('#csAddAddrHint').innerText()), await page.locator('#csAddAddrHint').innerText().catch(() => ''));
  const payM = await page.evaluate(() => window.__cs.payload());
  check('M the payload is the connection a save sends (ConnformModel.wire)', JSON.stringify(payM) === JSON.stringify({ protocol: 'rtu', serial_port: '/dev/ttyQA1',
        baudrate: 19200, parity: 'E', stopbits: 1, bytesize: 8, unit_id: 1, timeout: 3 }) && (await addErr()).length === 0, [payM, await addErr()]);
  // Back: the answers kept
  await page.evaluate(() => { window.__st = window.__cs.state(); });
  await page.evaluate(t => { window.__cs = window.mountConnStep(document.getElementById('csAddHost'), { context: 'add', template: t, state: window.__st }); }, sdm);
  await page.waitForTimeout(600);
  check('M Back: the answers kept', JSON.stringify(await page.evaluate(() => window.__cs.payload())) === JSON.stringify(payM));
  // Seplos on a listened bus: the role said, never chosen (M7)
  await page.evaluate(t => { window.__cs = window.mountConnStep(document.getElementById('csAddHost'), { context: 'add', template: t, prefill: { protocol: 'seplos_v2', serial_port: '/dev/ttyQA0' } }); }, sv);
  await page.waitForFunction(() => !document.querySelector('#csAddHost .cs-loading'));
  await page.waitForTimeout(400);
  check('M Seplos on a listened bus: no role chosen, Master refused (tap1), Listen-only said', (await page.evaluate(() => document.querySelector('input[name="csAddRole"]:checked'))) === null
        && (await page.getAttribute('#csAddRole-poll', 'aria-disabled')) === 'true' && /Bus listener/.test(await page.locator('#csAddRoleQ').innerText())
        && (await addErr()).includes('connection.mode'), [await addErr(), await page.locator('#csAddRoleQ').innerText().catch(() => '')]);
  await page.evaluate(() => document.getElementById('csAddHost')?.remove());
  check('no native dialog, no page error', page.dlg.native === 0 && page.errs.length === 0 && ro.errs.length === 0 && phone.errs.length === 0,
        [...page.errs, ...ro.errs, ...phone.errs].join(' | '));
} catch (e) {
  check('ran to the end', false, String(e && e.stack || e).slice(0, 600));
} finally {
  for (const id of CREATED) await api.delete(`/api/devices/${id}`);
  // rtu1's line back as it was
  const etag = (await api.get('/api/devices/rtu1')).headers().etag;
  await api.patch('/api/devices/rtu1', { data: { sections: { connection: { protocol: 'rtu', baudrate: rtu1Line.baudrate || 9600,
    parity: rtu1Line.parity || 'N', stopbits: rtu1Line.stopbits || 1, bytesize: rtu1Line.bytesize || 8 } } }, headers: { 'If-Match': etag } });
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
