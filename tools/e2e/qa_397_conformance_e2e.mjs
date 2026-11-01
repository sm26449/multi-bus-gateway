// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Release 3.97 — design conformance, as an operator meets it in the web UI.
// One check per user-visible decision of ADR-0025..0029 (and the device-model
// design's answers), each named after the decision it holds:
//
//   A  ADR-0025 §4   a new device is write-locked; the Write protection card
//                    says so; an admin unlocks it; the raw-write refusal
//                    points at where the card is
//   B  ADR-0025 §1   Test / SunSpec walk / bridge bus scan on a listened bus
//                    refused, the listeners named, before any port is opened
//                    (the fixture's serial paths do not exist)
//   C  ADR-0025 §1   a poll cannot be saved onto a listened bus, nor a
//                    listener onto a polled one — at the field (Add's
//                    Connection step: the role refused with its reason;
//                    the device page; the bridge's bus facts)
//   D  ADR-0026 §3   Settings → Gateway identity: labels, help (restore
//                    included), scope behaviour for new vs existing devices
//   E  ADR-0026 §4   a refused topic prefix at its field in Add Device
//   F  ADR-0026 §6   two tabs on one device: the second save is told
//   G  ADR-0028      the serial port picker: not-mapped lines, Advanced,
//                    Refresh, a listened port refused with its reason
//   H  ADR-0027 1-3  the type catalog, "+ Add type" (type dialog and
//                    meter-only), Find facets, template filter, editor
//   I  ADR-0027 4    identity attributes: edit, validation, the published
//                    meta (read from a throwaway broker), privacy switches
//   J  ADR-0029      the InfluxDB heartbeat in Settings and on Outputs
//   K  ADR-0021/22   the removed switches leave no trace
//   L  ADR-0025 §6   HA discovery follows the device's MQTT output
//   M  i18n          every new string in en and ro
//
// A throwaway gateway on the `qa` fixture (127.0.0.1 only, nothing real
// reachable), with a throwaway broker in its network namespace so the
// published meta can be read (QA_BROKER; without it the meta checks are
// skipped and said so):
//
//   MBG_FX_NAME=qa3-a tools/e2e/fixture_up.sh qa 18431
//   docker run -d --rm --name qa3-broker --network container:qa3-a eclipse-mosquitto:2.1.2-alpine
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18431 QA_BROKER=qa3-broker \
//     node tools/e2e/qa_397_conformance_e2e.mjs        [ONLY=A,B,…]
//   docker rm -f qa3-broker qa3-a
//
// It writes (devices, types, attributes, settings): start the fixture fresh.
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab, deviceAction, editorSection, editorSettled, saveDeviceEditor } from './lib/device-page.mjs';
import { openAdd, pickModel, answer, fill, next, step } from './lib/add-device.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18431';
const BROKER = process.env.QA_BROKER || '';
const ONLY = (process.env.ONLY || '').split(',').filter(Boolean);
const OUT = process.env.OUT || '';
const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + String(extra).slice(0, 400) : ''}`);
};
const skip = name => console.log(`SKIP ${name}`);
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  return { status: r.status, body, headers: r.headers };
};
const device = async id => (await api(`/api/devices/${id}`)).body?.device || {};
const settle = (page, ms = 450) => page.waitForTimeout(ms);
// the retained meta of a device, from the throwaway broker
const meta = (topic) => {
  const out = execFileSync('docker', ['exec', BROKER, 'mosquitto_sub', '-h', '127.0.0.1', '-t', topic, '-C', '1', '-W', '6'],
                          { encoding: 'utf8', timeout: 15000 });
  return JSON.parse(out.trim().split('\n').pop());
};
const retainedCount = (filter) => {
  let out = '';
  try {
    out = execFileSync('docker', ['exec', BROKER, 'mosquitto_sub', '-h', '127.0.0.1', '-t', filter, '-W', '3', '-v'],
                       { encoding: 'utf8', timeout: 15000 });
  } catch (e) { out = String(e.stdout || ''); }       // -W ends with a timeout status
  return out.split('\n').filter(l => l.trim() && !/\s$/.test(l) && l.split(' ').length > 1).length;
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(path = '/', { lang = 'en', viewport = { width: 1440, height: 950 } } = {}) {
  const ctx = await browser.newContext({ viewport });
  await ctx.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, lang);
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|status of (4\d\d|5\d\d)/.test(m.text())) page.errs.push(m.text()); });
  page.codes = [];
  page.on('response', r => { if (r.request().method() !== 'GET') page.codes.push([r.request().method(), new URL(r.url()).pathname, r.status()]); });
  page.dlg = await watchDialogs(page);
  await page.goto(BASE + path);
  await page.waitForLoadState('load');
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 45000 });
  await page.evaluate(() => { window.app._firstRunShown = true; window.app.closeModal?.('firstRunModal'); });
  return page;
}
const shot = async (page, name) => { if (OUT) await page.screenshot({ path: `${OUT}/${name}.png` }); };
const describedBy = async (page, sel) => {
  const ids = ((await page.getAttribute(sel, 'aria-describedby')) || '').split(/\s+/).filter(Boolean);
  const texts = [];
  for (const id of ids) texts.push(await page.locator('#' + id).innerText().catch(() => ''));
  return texts.join(' ');
};
const toastText = async page => (await page.locator('.toast').allInnerTexts().catch(() => [])).join(' | ');

// Add Device (L3.9), By connection: RS-485 on a port of this host (where:
// 'here') or through a bridge ('bridge'), Modbus RTU, the role when given
async function wizardRtu(page, where = 'here', role = '') {
  await openAdd(page);
  await page.click('[data-ad-way="connection"]');
  await answer(page, 'PortKind', 'serial');
  await answer(page, 'Reach', where);
  await answer(page, 'Speaks', 'modbus:rtu').catch(() => {});
  if (role) await answer(page, 'Role', role).catch(() => {});
  await settle(page, 600);
}
async function typePath(page, path, px = 'csAdd') {
  await page.evaluate(px => { const d = document.getElementById(px + 'SerialAdv'); if (d) d.open = true; }, px);
  await page.fill(`#${px}Serial`, path);
  await page.locator(`#${px}Serial`).blur();
  await settle(page, 600);
}
const closeAdd = page => page.evaluate(() => { if (window.app._ad) window.app._ad.done = true; window.app.closeModal('addDeviceModal'); });
const roleWhy = async (page, role) => {
  const id = await page.getAttribute(`#csAddRole-${role}`, 'aria-describedby');
  return id ? await page.locator('#' + id).innerText().catch(() => '') : '';
};
// Add Device: the model, Ethernet at 127.0.0.1, the check (nothing answers:
// Next goes on without one), onto Name & save
async function wizardTcpToStep3(page, unit, tpl = 'eastron_sdm120') {
  await openAdd(page);
  await pickModel(page, tpl, tpl);
  await answer(page, 'PortKind', 'ethernet');
  await answer(page, 'Reach', 'address').catch(() => {});
  await fill(page, 'Host', '127.0.0.1');
  await fill(page, 'Port', '15990');
  await fill(page, 'Unit', unit);
  await next(page);
  await next(page);
  await page.waitForSelector('#adOutPrefix', { state: 'attached' });
}
async function openDevice(page, id, tab) {
  await page.evaluate(id => window.app.openDeviceDetail(id), id);
  await page.waitForSelector('#deviceWsTabs');
  if (tab) await openDeviceTab(page, tab);
}

const SECTIONS = {};

// ── A. ADR-0025 §4: a new device is write-locked ───────────────────────────
SECTIONS.A = async () => {
  await api('/api/devices/qa-new', { method: 'DELETE' });
  const page = await open('/#/devices');
  await wizardTcpToStep3(page, 9);
  await page.fill('#adName', 'QA new');
  await page.fill('#adId', 'qa-new');
  await page.locator('#adId').dispatchEvent('input');
  if (await page.isChecked('#adEnabled')) await page.click('label:has(#adEnabled)');
  check('A0 Name & save says it is created write-locked, unlocked by an admin from its ⋯ → Unlock writes…',
        /write-locked/.test(await page.locator('#adWrites').innerText()) && /Unlock writes/.test(await page.locator('#adWrites').innerText()));
  await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 }).catch(() => {});
  const sent = page.waitForRequest(r => r.method() === 'POST' && new URL(r.url()).pathname === '/api/devices' && !/dry_run/.test(r.url()));
  await page.click('#addDevNext');
  const body = JSON.parse((await sent).postData() || '{}');
  await page.waitForSelector('#adResult');
  check('A1 Add Device sends no write_locked: the server decides', !('write_locked' in body));
  check('A1b the result says it: Created · … · write-locked', /write-locked/.test(await page.locator('#adResultState').innerText()));
  await page.click('#adResDone');
  check('A2 a device created in Add Device is stored write-locked', (await device('qa-new')).write_locked === true);
  const forced = await api('/api/devices', { method: 'POST', body: JSON.stringify({
    id: 'qa-api', name: 'QA api', template: 'eastron_sdm120', enabled: false, write_locked: false,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15502, unit_id: 11 } }) });
  check('A3 the API stores a new device locked whatever the payload says',
        forced.status < 300 && (await device('qa-api')).write_locked === true, JSON.stringify(forced.body).slice(0, 200));
  check('A4 an existing device keeps its stored value (nothing migrated)', (await device('em1')).write_locked !== true);

  // L3.3: the lock is a pill in the header and a line in Setup; it is
  // switched from ⋯ — Unlock writes… asks in its own dialog, Lock acts at once
  await openDevice(page, 'qa-new', 'setup');
  const pill = page.locator('#ddvFacts .dev-sum-lock');
  await page.waitForSelector('[data-dpanel="setup"] [data-setup="writes"]');
  const setupText = await page.locator('[data-dpanel="setup"] [data-setup="writes"]').innerText();
  check('A5 the header and Setup show it locked', /writes locked/i.test(await pill.innerText())
        && /Write protection/.test(setupText) && /locked/i.test(setupText), `${await pill.innerText()} | ${setupText}`);
  await page.click('#ddvActions');
  const unlockItem = page.locator('#ddvActionsMenu [data-ddv-act="unlock"]');
  check('A7 Unlock writes… is a ⋯ item, admin-only', await unlockItem.isVisible() && await unlockItem.getAttribute('data-admin') !== null);
  await page.keyboard.press('Escape');
  // from the keyboard: ⋯, the arrows to Unlock writes…, Enter; its dialog
  await page.focus('#ddvActions');
  await page.keyboard.press('Enter');
  for (let i = 0; i < 20 && (await page.evaluate(() => document.activeElement?.dataset.ddvAct)) !== 'unlock'; i++) await page.keyboard.press('ArrowDown');
  page.dlg.answers.push(true);
  await page.keyboard.press('Enter');
  await settle(page, 900);
  const asked = page.dlg.seen[page.dlg.seen.length - 1] || {};
  check('A6 its dialog says a new device starts locked', /Unlock writes/i.test(asked.title || '') && /new device starts locked/i.test(asked.message || ''),
        JSON.stringify(asked).slice(0, 300));
  check('A8 an admin unlocks it from the keyboard', (await device('qa-new')).write_locked === false && /writes allowed/i.test(await toastText(page)),
        await toastText(page));
  check('A9 the header\'s pill follows: "writes allowed", no longer "locked"',
        /writes allowed/i.test(await pill.innerText()), await pill.innerText());
  check('A10 the focus is back on ⋯', await page.evaluate(() => document.activeElement?.id) === 'ddvActions',
        await page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName));
  const before = page.dlg.seen.length;
  await page.click('#ddvActions');
  await page.click('#ddvActionsMenu [data-ddv-act="lock"]');
  await settle(page, 900);
  check('A11 …and Lock writes locks it again, at once (no dialog)', (await device('qa-new')).write_locked === true
        && /writes locked/i.test(await pill.innerText()) && page.dlg.seen.length === before);
  // the raw-write refusal points at where the lock is switched
  const where = await page.evaluate(() => window.app.t('write.locked'));
  check('A12 the raw-write refusal names where it is unlocked (⋯ → Unlock writes…)',
        /Unlock writes/.test(where) && !/tab/.test(where), where);
  await shot(page, 'A-writelock');
  check('A13 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
  await api('/api/devices/qa-api', { method: 'DELETE' });
};

// ── B. ADR-0025 §1: nothing is transmitted on a listened bus ──────────────
SECTIONS.B = async () => {
  const page = await open('/#/devices');
  // Add's Connection step (L3.9): on a listened bus Master is not offered at
  // all — aria-disabled, the listener named — so nothing can be Tested as a
  // poll there; the server refuses such a Test anyway (asked directly)
  await wizardRtu(page, 'here');
  await typePath(page, '/dev/ttyQA0');
  const w1 = await roleWhy(page, 'poll');
  check('B1 Add: Master on a listened serial bus is refused, the listener named', await page.getAttribute('#csAddRole-poll', 'aria-disabled') === 'true'
        && /tap1|Bus listener/.test(w1), w1);
  const tt = await api('/api/devices/test', { method: 'POST', body: JSON.stringify({ connection: { protocol: 'rtu', serial_port: '/dev/ttyQA0', unit_id: 1, baudrate: 9600 } }) });
  const t1 = JSON.stringify(tt.body);
  check('B2 a Test as a poll there is refused by the gateway, tap1 named, before the port is opened', /tap1/.test(t1) && !/No such file|could not open/i.test(t1), t1);
  check('B3 …Listen-only pre-filled by the bus\'s claim, with its reason', await page.isChecked('#csAddRole-listen')
        && /tap1|Bus listener/.test(await page.locator('#csAddRoleQ').innerText()));
  check('B4 …and no Test offered for a tap (it never transmits)', !(await page.locator('#csAddTestBtn').count()));
  await closeAdd(page);

  // through the bridge: the bus :14196 the tap brtap listens on
  await wizardRtu(page, 'bridge');
  await page.selectOption('#csAddBus', 'panel::14196');
  await settle(page);
  const w2 = await roleWhy(page, 'poll');
  check('B5 Add: Master through a bridge on a listened bus is refused, brtap named', await page.getAttribute('#csAddRole-poll', 'aria-disabled') === 'true'
        && /brtap|Bridge listener/.test(w2), w2);
  await closeAdd(page);

  // the listener's own Test still listens
  await openDevice(page, 'tap1', 'connection');
  await page.click('#ddvTestBtn');
  await page.waitForFunction(() => (document.getElementById('ddvTestResult')?.textContent || '').trim(), null, { timeout: 20000 });
  const t3 = await page.locator('#ddvTestResult').innerText();
  check('B6 the listening device\'s own Test is not refused (it listens)', !/would transmit/.test(t3), t3);

  // SunSpec walk (Diagnostics) on the bridge's listened bus
  await page.evaluate(() => window.app.navigateTo('diagnostics'));
  await page.waitForSelector('#ssHost', { state: 'visible' });
  await page.fill('#ssHost', '127.0.0.1');
  await page.fill('#ssPort', '14196');
  await page.click('#ssBtn');
  await page.waitForFunction(() => /would transmit|SunSpec @|failed|error/i.test(document.getElementById('ssResult')?.textContent || ''));
  const t4 = await page.locator('#ssResult').innerText();
  check('B7 the SunSpec walk on a listened bus is refused in the UI, brtap named', /brtap/.test(t4) && /Nothing was sent/.test(t4), t4);

  // a bridge's bus: Scan and Check mode are not offered where a tap listens, and the page says why
  await page.goto(BASE + '/#/devices');
  await page.waitForFunction(() => window.app && window.app._routeReady);
  await page.evaluate(() => window.app._showDevicesList?.());
  const port = page.locator('.bridge-port:has(code:text-is(":14196"))');
  await port.waitFor();
  check('B8 a listened bridge bus offers no Scan / Check mode', await port.locator('[data-action="busScan"], [data-action="bridgeProbe"]').count() === 0);
  check('B9 …and says why in words on the page (not only in a tooltip)', /only listens/.test(await port.locator('.bridge-tap-why').innerText().catch(() => '')));
  const scan = await api('/api/bridges/panel/ports/14196/scan', { method: 'POST', body: '{}' });
  check('B10 the API refuses the scan too, brtap named', scan.status === 409 && /brtap/.test(JSON.stringify(scan.body)));
  // the sweeps the UI has no button for (local RTU): the API answers the same
  const sw = await api('/api/discover/modbus/units', { method: 'POST', body: JSON.stringify({ protocol: 'rtu', serial_port: '/dev/ttyQA0', unit_start: 1, unit_end: 2 }) });
  check('B11 the RTU unit sweep on a listened bus: 409, tap1 named', sw.status === 409 && (sw.body.detail.listeners || []).includes('tap1'));
  const sw2 = await api('/api/discover/modbus/units', { method: 'POST', body: JSON.stringify({ protocol: 'tcp', host: '127.0.0.1', port: 14196, unit_start: 1, unit_end: 2 }) });
  check('B12 the TCP unit sweep on a listened bridge bus: 409, brtap named', sw2.status === 409 && (sw2.body.detail.listeners || []).includes('brtap'));
  check('B13 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

// ── C. ADR-0025 §1: a save that would poll a listened bus, or the reverse ──
SECTIONS.C = async () => {
  const page = await open('/#/devices');
  // a poll onto the tapped serial line: Master chosen first, then the tapped
  // line typed — the role the bus allows is said, and Next stays at Connection
  await wizardRtu(page, 'here', 'poll');
  await typePath(page, '/dev/ttyQA0');
  await fill(page, 'Unit', 4);
  await page.click('#addDevNext');
  await settle(page, 900);
  const roleQ = await page.locator('#csAddRoleQ').innerText();
  check('C1 Add Device: a poll on a tapped line is not taken there, tap1 named', /tap1|Bus listener/.test(roleQ)
        && await page.getAttribute('#csAddRole-poll', 'aria-disabled') === 'true', roleQ);
  check('C2 …Listen-only is what the bus leaves, or Next is held at Connection',
        await page.isChecked('#csAddRole-listen') || await step(page) === 'connection');
  const sentPoll = await api('/api/devices?dry_run=true&only=connection', { method: 'POST', body: JSON.stringify({ connection: { protocol: 'rtu', serial_port: '/dev/ttyQA0', unit_id: 4, baudrate: 9600 } }) });
  check('C3 …and the gateway refuses such a poll at the serial port, tap1 named', sentPoll.status === 422
        && /tap1/.test(JSON.stringify(sentPoll.body)) && Object.keys(sentPoll.body.detail?.fields || {}).some(k => /serial_port|connection/.test(k)), JSON.stringify(sentPoll.body).slice(0, 300));
  await closeAdd(page);
  // a listener onto the polled serial line
  await wizardRtu(page, 'here');
  await typePath(page, '/dev/ttyQA1');
  const w4 = await roleWhy(page, 'listen');
  check('C4 Add Device: listening on a polled line is refused, rtu1 named', await page.getAttribute('#csAddRole-listen', 'aria-disabled') === 'true' && /rtu1|Bus poller/.test(w4), w4);
  await closeAdd(page);

  // through the bridge: the bus facts say the bus is listened before Next
  await wizardRtu(page, 'bridge');
  await page.selectOption('#csAddBus', 'panel::14196');
  await settle(page);
  const hint = await page.locator('#csAddBusQ').innerText();
  check('C5 the bridge bus says it is listened to (not "they take turns")',
        /brtap|Bridge listener/.test(hint) && /listen/i.test(hint) && !/take turns/.test(hint), hint);
  const w6 = await roleWhy(page, 'poll');
  check('C6 polling through the bridge on a listened bus is refused at the role, brtap named', /brtap|Bridge listener/.test(w6)
        && await page.getAttribute('#csAddRole-poll', 'aria-disabled') === 'true', w6);
  await closeAdd(page);

  // a listener on a bridge bus another device polls: said before Next
  await wizardRtu(page, 'bridge');
  await page.selectOption('#csAddBus', 'panel::14197');
  await settle(page);
  const note = await page.locator('#csAddBusQ').innerText().catch(() => '');
  check('C7 listen-only on a polled bridge bus: the bus says who polls it, before Next', /Bridge poller|brpoll/.test(note), note);
  const w8 = await roleWhy(page, 'listen');
  check('C8 …and Listen-only is refused there, brpoll named', /brpoll|Bridge poller/.test(w8) && await page.getAttribute('#csAddRole-listen', 'aria-disabled') === 'true', w8);
  await closeAdd(page);

  // the device page: rtu1 moved onto the tapped line
  await openDevice(page, 'rtu1', 'connection');
  await page.waitForSelector('#ddvSerialList');
  await typePath(page, '/dev/ttyQA0', 'ddv');
  // the Edit modal (L3.4): its dry run asks the gateway before Save
  await editorSettled(page);
  await page.click('#devEditSave', { force: true });
  await page.waitForSelector('#ddvSerial-err', { state: 'attached', timeout: 10000 }).catch(() => {});
  const e4 = await page.locator('#ddvSerial-err').innerText().catch(() => '');
  // since L3.6 the refusal is the role's (the bus is listened: Master is not
  // the bus's to give), said at the role or in the dialog's error list
  const e4all = [e4, await page.locator('#devEditErrors').innerText().catch(() => ''),
                 await page.locator('#ddvRoleQ').innerText().catch(() => '')].join(' ');
  check('C9 the device page: moving a poller onto a tapped line is refused, the listener (tap1) named', /tap1|Bus listener/.test(e4all), e4all);
  check('C10 …nothing stored', (await device('rtu1')).connection?.serial_port === '/dev/ttyQA1' || (await device('rtu1')).serial?.serial_port === '/dev/ttyQA1');
  check('C11 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

// ── D. ADR-0026 §3: Settings → Gateway identity ───────────────────────────
SECTIONS.D = async () => {
  const before = (await api('/api/config/identity')).body;
  check('D1 an older install (no identity_scope in its file) is legacy', before.identity_scope === 'legacy', JSON.stringify(before));
  const page = await open('/');
  await page.evaluate(() => window.app.openSettingsTab('general'));
  await page.waitForSelector('#cfgGatewayId', { state: 'visible' });
  await page.waitForFunction(() => document.getElementById('cfgIdentityExTopic')?.textContent.trim());
  check('D2 the id and the scope have labels', (await page.locator('label[for="cfgGatewayId"]').innerText()).trim() === 'Gateway id'
        && /How new devices are named/.test(await page.locator('label[for="cfgIdentityScope"]').innerText()));
  const idHint = await describedBy(page, '#cfgGatewayId');
  const scHint = await describedBy(page, '#cfgIdentityScope');
  check('D3 the help says the rule of an id and that existing devices keep their names',
        /Lowercase letters/.test(idHint) && /keeps all three/.test(idHint) && /only devices created afterwards/.test(scHint), idHint + ' / ' + scHint);
  check('D4 the help says a restore or an import keeps this gateway\'s id and scope (ADR-0026 §3)',
        /restore/i.test(idHint + scHint) && /import/i.test(idHint + scHint), idHint + ' / ' + scHint);
  // switch to per-gateway, with the keyboard
  await page.fill('#cfgGatewayId', 'qa-gw');
  await page.focus('#cfgIdentityScope');
  await page.selectOption('#cfgIdentityScope', 'gateway');
  await page.click('[data-action="saveIdentityConfig"]');
  await page.waitForFunction(() => /Saved/.test(document.getElementById('identitySaveFeedback')?.textContent || ''));
  check('D5 the example shows where a new device goes', /mbg\/qa-gw\/devices\/\{device\}/.test(await page.locator('#cfgIdentityExample').innerText()));
  const made = await api('/api/devices', { method: 'POST', body: JSON.stringify({ id: 'qa-scoped', name: 'QA scoped', template: 'eastron_sdm120', enabled: false,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15502, unit_id: 12 } }) });
  const d = await device('qa-scoped');
  check('D6 a device created now: mbg/qa-gw/devices/<id>, bucket qa-gw-<id>, its HA node stored',
        made.status < 300 && d.mqtt_topic_prefix === 'mbg/qa-gw/devices/qa-scoped' && d.influxdb_bucket === 'qa-gw-qa-scoped',
        JSON.stringify({ p: d.mqtt_topic_prefix, b: d.influxdb_bucket }));
  const e1 = await device('em1');
  check('D7 an existing device keeps its topic and bucket', e1.mqtt_topic_prefix === 'mbg/devices/em1', e1.mqtt_topic_prefix);
  // back: a later scope change leaves qa-scoped as it was
  await page.selectOption('#cfgIdentityScope', 'legacy');
  await page.click('[data-action="saveIdentityConfig"]');
  await page.waitForFunction(() => /Saved/.test(document.getElementById('identitySaveFeedback')?.textContent || ''));
  check('D8 switching back names only new devices: qa-scoped keeps its topic', (await device('qa-scoped')).mqtt_topic_prefix === 'mbg/qa-gw/devices/qa-scoped');
  // ro
  await page.evaluate(() => window.app.setLanguage('ro'));
  await settle(page, 600);
  await page.evaluate(() => window.app.openSettingsTab('general'));
  await page.waitForSelector('#cfgGatewayId', { state: 'visible' });
  const roHint = await describedBy(page, '#cfgIdentityScope') + ' ' + await describedBy(page, '#cfgGatewayId');
  check('D9 ro: the card and its help in Romanian', /Identitatea gateway-ului/.test(await page.locator('[data-i18n="identity.title"]').first().innerText())
        && /restaur/i.test(roHint), roHint);
  await page.evaluate(() => window.app.setLanguage('en'));
  check('D10 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
  await api('/api/devices/qa-scoped', { method: 'DELETE' });
};

// ── E. ADR-0026 §4: a refused topic prefix, at its field ──────────────────
SECTIONS.E = async () => {
  const page = await open('/#/devices');
  const cases = [
    ['mbg/devices/em1', /em1/, 'the same as another device\'s'],
    ['mbg/devices/em1/sub', /inside device 'em1'/, 'inside another device\'s'],
    ['mbg/devices', /em1|holds|contains/, 'holding other devices\''],
    ['mbg/+/x', /\+|wildcard/, 'with a wildcard'],
    ['mbg/gateway', /gateway/, 'the gateway\'s own root'],
  ];
  let n = 1;
  for (const [topic, rx, what] of cases) {
    await wizardTcpToStep3(page, 20 + n);
    await page.fill('#adName', 'Prefix ' + n);
    await page.locator('#adName').dispatchEvent('input');
    await page.click('#adOutMqtt summary');
    await page.fill('#adOutPrefix', topic);
    await page.locator('#adOutPrefix').dispatchEvent('input');
    await page.waitForFunction(() => document.getElementById('adOutPrefix')?.getAttribute('aria-invalid') === 'true', null, { timeout: 10000 }).catch(() => {});
    const err = await describedBy(page, '#adOutPrefix');
    check(`E${n} a prefix ${what} is refused at the topic field (Create waits)`, rx.test(err) && await step(page) === 'save'
          && await page.getAttribute('#addDevNext', 'aria-disabled') === 'true', `${topic}: ${err}`);
    await closeAdd(page);
    n++;
  }
  check(`E${n} nothing was created`, !((await api('/api/devices')).body.devices || []).some(d => /^prefix-/.test(d.id)));
  check(`E${n + 1} no page errors`, !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

// ── F. ADR-0026 §6: two tabs on one device ────────────────────────────────
SECTIONS.F = async () => {
  const tagged = await fetch(BASE + '/api/devices/em2');
  const tag = tagged.headers.get('etag');
  check('F1 GET one device carries an ETag', !!tag);
  const a = await open('/#/devices');
  const b = await open('/#/devices');
  // both tabs in the Edit modal (L3.4)
  await openDevice(a, 'em2', 'connection');
  await openDevice(b, 'em2', 'connection');
  await editorSection(a, 'identity');
  await a.fill('#ddvName', 'Garage meter (tab A)');
  await saveDeviceEditor(a);
  check('F2 tab A saves', (await device('em2')).name === 'Garage meter (tab A)');
  const n = b.codes.length;
  await b.fill('#ddvUnit', '5');
  await b.waitForSelector('#devEditConflict .dev-shell-conflictbox', { timeout: 10000 }).catch(() => {});
  const fb = await b.locator('#devEditConflict').innerText();
  const puts = b.codes.slice(n).filter(([m, p]) => m === 'PATCH' && p === '/api/devices/em2');
  const after = await device('em2');
  check('F3 tab B is told the device changed meanwhile (412), and tab A\'s name is not overwritten',
        puts.some(([, , s]) => s === 412) && /changed/i.test(fb) && after.name === 'Garage meter (tab A)',
        `${JSON.stringify(puts)} feedback="${fb}" name now="${after.name}"`);
  // ADR-0030 / L3.4: the modal says it (role=alert); its edits are kept, re-applied
  check('F3b tab B\'s modal shows the "changed elsewhere" banner (role=alert); its edit is kept',
        await b.locator('#devEditConflict[role="alert"] .dev-shell-conflictbox').isVisible()
        && (await b.inputValue('#ddvUnit')) === '5');
  // the API itself
  const stale = await api('/api/devices/em2', { method: 'PUT', headers: { 'If-Match': tag }, body: JSON.stringify({ id: 'em2', name: 'x' }) });
  check('F4 the API answers 412 to a stale If-Match, with the current entry', stale.status === 412 && !!stale.body.detail.current);
  await a.context().close(); await b.context().close();
  await api('/api/devices/em2', { method: 'PUT', body: JSON.stringify({ id: 'em2', name: 'Garage meter', connection: { unit_id: 2 } }) });
};

// ── G. ADR-0028: the serial port picker ───────────────────────────────────
SECTIONS.G = async () => {
  const real = (await api('/api/serial-ports')).body;
  const page = await open('/#/devices');
  await wizardRtu(page, 'here', 'poll');
  await page.waitForSelector('#csAddSerialList');
  const rows = await page.locator('#csAddSerialList .cf-port-row').count();
  check('G1 the picker lists the ports this host has', rows === (real.ports || []).length, `${rows} vs ${real.ports?.length}`);
  const nm = (real.ports || []).find(p => p.state === 'not_mapped');
  if (nm) {
    const txt = await page.locator(`#csAddSerialList .cf-port-row[data-port="${nm.name}"]`).innerText();
    check('G2 a port not mapped shows the compose line and the --device flag', txt.includes(nm.fix.compose) && txt.includes(nm.fix.docker_run), txt);
    const b = page.locator(`#csAddSerialList .cf-port-row[data-port="${nm.name}"] button.cf-port`);
    check('G3 …and cannot be picked (aria-disabled, still focusable)', await b.getAttribute('aria-disabled') === 'true' && !(await b.evaluate(e => e.disabled)));
  } else skip('G2/G3 no port is not-mapped on this host');
  const nothingPickable = (real.ports || []).every(p => p.state === 'not_mapped');
  if (nothingPickable) check('G4 nothing can be picked → "Advanced: type a path" is open', await page.getAttribute('#csAddSerialAdv', 'open') !== null);
  check('G5 the path field under Advanced is labelled and explained', await page.locator('label[for="csAddSerial"]').count() === 1
        && /udev alias/.test(await page.locator('#csAddSerialAdv').innerText()));
  await page.click('#csAddSerialRefresh');
  await page.waitForFunction(() => /found/.test(document.getElementById('csAddSerialStatus')?.textContent || ''));
  check('G6 Refresh says how many ports it found', /serial port/.test(await page.locator('#csAddSerialStatus').innerText()));
  await closeAdd(page);
  await page.context().close();

  // a port another device listens to: faked, so the state comes from the API shape
  const p2 = await open('/#/devices');
  await p2.route('**/api/serial-ports*', r => r.fulfill({ json: { hidden: 1, ports: [
    { name: 'ttyQA0', dev: '/dev/ttyQA0', path: '/dev/ttyQA0', stable_path: '/dev/ttyQA0', by_id: [], kind: 'hat', label: 'QA HAT', model: '',
      state: 'listened', listened_by: ['tap1'], polled_by: [], reason: 'listened to by tap1', mapped: true, fix: null, processes: [] },
    { name: 'ttyQA1', dev: '/dev/ttyQA1', path: '/dev/ttyQA1', stable_path: '/dev/ttyQA1', by_id: [], kind: 'hat', label: 'QA HAT', model: '',
      state: 'polled', listened_by: [], polled_by: ['rtu1'], reason: 'polled by rtu1', mapped: true, fix: null, processes: [] }] } }));
  await p2.evaluate(() => { window.app._serialPorts = null; });
  await wizardRtu(p2, 'here', 'poll');
  await p2.waitForSelector('#csAddSerialList .cf-port-row[data-port="ttyQA0"]');
  const lb = p2.locator('#csAddSerialList .cf-port-row[data-port="ttyQA0"] button.cf-port');
  const why = await describedBy(p2, '#csAddSerialList .cf-port-row[data-port="ttyQA0"] button.cf-port');
  check('G7 a listened port cannot be polled: aria-disabled, its reason names the listener', await lb.getAttribute('aria-disabled') === 'true' && /tap1/.test(why), why);
  check('G8 the console is said hidden', /console/.test(await p2.locator('#csAddSerialList').innerText()));
  await answer(p2, 'Role', 'listen');
  await p2.waitForSelector('#csAddSerialList .cf-port-row[data-port="ttyQA1"]');
  check('G9 Listen-only: the polled port is the one refused', await p2.locator('#csAddSerialList .cf-port-row[data-port="ttyQA1"] button.cf-port').getAttribute('aria-disabled') === 'true'
        && !(await p2.locator('#csAddSerialList .cf-port-row[data-port="ttyQA0"] button.cf-port').getAttribute('aria-disabled')));
  await closeAdd(p2);
  // the device page uses the same picker
  await openDevice(p2, 'rtu1', 'connection');
  check('G10 the device page has the picker too', await p2.locator('#ddvSerialList .cf-port-row').count() === 2);
  await p2.evaluate(() => window.app.setLanguage('ro'));
  await settle(p2, 500);
  await openDevice(p2, 'rtu1', 'connection');
  await p2.waitForSelector('#ddvSerialList .cf-port-row');
  check('G11 ro: states and Advanced in Romanian', /Ascultat|Interogat/.test(await p2.locator('#ddvSerialPick').innerText()) && /Avansat/.test(await p2.locator('#ddvSerialPick').innerText()));
  await p2.evaluate(() => window.app.setLanguage('en'));
  check('G12 no page errors', !page.errs.length && !p2.errs.length, [...page.errs, ...p2.errs].join(' | '));
  await p2.context().close();
};

// ── H. ADR-0027 §1-3: one type catalog ────────────────────────────────────
SECTIONS.H = async () => {
  for (const id of ['qa_chiller', 'qa_boiler', 'qa_pump']) await api(`/api/device-types/${id}`, { method: 'DELETE' });
  const page = await open('/#/templates');
  await page.click('#tmTabs [data-tmtab="types"]');
  await page.waitForSelector('#tmTypesPanel table');
  const builtins = ['meter', 'inverter', 'battery', 'site', 'heat_pump', 'drive', 'ups', 'charger', 'sensor', 'converter', 'other'];
  const listed = await page.locator('#tmTypesPanel [data-type-row]').evaluateAll(els => els.map(e => e.dataset.typeRow));
  check('H1 the catalog tab lists every built-in kind (converter included)', builtins.every(k => listed.includes(k)), listed.join(','));
  page.dlg.answers.push({ id: 'qa_chiller', en: 'QA chiller', ro: 'Răcitor QA', icon: 'snow', behaves_like: 'heat_pump' });
  await page.focus('#typeAddBtn');
  await page.keyboard.press('Enter');
  await page.waitForSelector('#tmTypesPanel [data-type-row="qa_chiller"]');
  check('H2 a type of one\'s own is added from the keyboard (id, en, ro, icon, behaves like)',
        JSON.stringify(page.dlg.seen.at(-1).fields.map(f => f.name)) === JSON.stringify(['id', 'en', 'ro', 'icon', 'behaves_like']));
  const tplBtn = page.locator('#tmTypesPanel [data-type-row="qa_chiller"] [data-action="typeTemplatesToggle"]');
  await tplBtn.click();
  await page.waitForSelector('#typeTpl_qa_chiller:not([hidden])');
  const viaApi = (await api('/api/device-templates?type=qa_chiller')).body.templates.map(t => t.id);
  check('H3 the templates for a type: its built-in kind\'s (heat pump maps), not a meter\'s',
        viaApi.length > 0 && !viaApi.includes('eastron_sdm630') && /Samsung/i.test(await page.locator('#typeTpl_qa_chiller').innerText()), viaApi.join(','));
  // the template editor: kind and "also suits"
  await page.evaluate(() => window.app.openTplEditor('eastron_sdm120', true));
  await page.waitForSelector('#tplKind');
  check('H4 the template editor shows the map\'s kind, labelled and explained', await page.inputValue('#tplKind') === 'meter'
        && await page.locator('label[for="tplKind"]').count() === 1 && !!(await describedBy(page, '#tplKind')));
  check('H5 …and offers our type under "also suits these types"', await page.locator('[data-tpl-type="qa_chiller"]').count() === 1);
  await page.evaluate(() => window.app.closeModal?.('tplEditorModal'));
  await page.context().close();

  // a device's type: Type… — the Edit modal's Identity, with "+ Add type" (L3.4)
  const p2 = await open('/#/devices/em2');
  await p2.waitForSelector('#ddvActions');
  await deviceAction(p2, '#ddvSetKind');
  await p2.waitForSelector('#devEditModal.active #devEditType');
  check('H6 the type choice offers the user type', await p2.locator('#devEditType option[value="qa_chiller"]').count() === 1);
  await p2.focus('#deTypeAddBtn');
  await p2.keyboard.press('Enter');
  await p2.waitForSelector('#deTypeForm:not([hidden])');
  await p2.keyboard.type('QA pump');
  await p2.fill('#deTnRo', 'Pompă QA');
  await p2.locator('#deTnId').fill('qa_pump');
  await p2.locator('#deTnId').press('Enter');
  await p2.waitForFunction(() => document.getElementById('devEditType')?.value === 'qa_pump');
  check('H7 "+ Add type" beside the type creates and picks it, from the keyboard', (await api('/api/device-types')).body.types.some(t => t.id === 'qa_pump'));
  await p2.selectOption('#devEditType', 'qa_chiller');
  await saveDeviceEditor(p2);
  await settle(p2);
  const d = ((await api('/api/devices')).body.devices || []).find(x => x.id === 'em2');
  check('H8 the device has the type; on the wire its kind is what it behaves like', d.type === 'qa_chiller' && d.kind === 'heat_pump' && d.type_label === 'QA chiller',
        JSON.stringify({ t: d.type, k: d.kind, l: d.type_label }));
  // meter-only: "what is it?" with "+ Add type", reached the way a user does
  await openAdd(p2);
  await p2.click('[data-ad-way="measured"]');
  await p2.waitForSelector('#adTypeAddBtn');
  check('H9 meter-only: our types are offered as "what it is"', await p2.locator('input[name="adKind"][value="qa_chiller"]').count() === 1);
  await p2.click('#adTypeAddBtn');
  await p2.fill('#adTnEn', 'QA boiler');
  await p2.fill('#adTnId', 'qa_boiler').catch(() => {});
  await p2.click('#adTnCreate');
  await p2.waitForSelector('input[name="adKind"][value="qa_boiler"]:checked', { state: 'attached' });
  check('H10 meter-only: "+ Add type" creates and picks it', await p2.evaluate(() => window.app._ad.s.kind) === 'qa_boiler');
  await closeAdd(p2);
  await p2.context().close();

  // Find: type and label facets
  await api('/api/attributes/devices/em2/identity', { method: 'PUT', body: JSON.stringify({ labels: ['qa-label'] }) });
  const p3 = await open('/#/devices');
  await p3.waitForSelector('#devFindQ');
  await p3.click('.find-bar[data-find="devices"] .find-facets-toggle').catch(() => {});
  const chip = p3.locator('.find-chip[data-facet="kind"][data-value="qa_chiller"]');
  await chip.waitFor();
  await chip.focus();
  await p3.keyboard.press('Enter');
  await settle(p3);
  const vis = await p3.locator('#devicesList [data-f]:visible').evaluateAll(els => [...new Set(els.map(e => e.dataset.f))]);
  check('H11 Find: the type facet shows only that type, said pressed', JSON.stringify(vis) === '["em2"]' && await chip.getAttribute('aria-pressed') === 'true', vis.join(','));
  await chip.click();
  await settle(p3);
  const lchip = p3.locator('.find-chip[data-facet="label"][data-value="qa-label"]');
  await lchip.waitFor();
  await lchip.click();
  await settle(p3);
  const vis2 = await p3.locator('#devicesList [data-f]:visible').evaluateAll(els => [...new Set(els.map(e => e.dataset.f))]);
  check('H12 Find: the label facet', JSON.stringify(vis2) === '["em2"]', vis2.join(','));
  await p3.context().close();

  // installation group roles come from the catalog
  const p4 = await open('/#/devices');
  await p4.evaluate(() => window.app.openEndpointDetail('sunfield'));
  await p4.waitForSelector('[data-action="openGroupModal"]');
  await p4.locator('[data-action="openGroupModal"][data-args=\'["sunfield","inverters"]\']').click();
  await p4.waitForSelector('#grpRole');
  const roles = await p4.locator('#grpRole option').evaluateAll(os => os.map(o => o.value));
  check('H13 an installation group\'s role is picked from the catalog (our types included), labelled',
        roles.includes('qa_chiller') && roles.includes('converter') && !roles.includes('other') && await p4.locator('label[for="grpRole"]').count() === 1, roles.join(','));
  const refusedRole = await api('/api/endpoints/sunfield', { method: 'GET' });
  void refusedRole;
  const bad = await api('/api/endpoints/sunfield', { method: 'GET' });
  check('H14 a type in use is not deleted; the device is named',
        await (async () => { const r = await api('/api/device-types/qa_chiller', { method: 'DELETE' }); return r.status === 409 && /em2/.test(JSON.stringify(r.body)); })());
  await p4.context().close();
  void bad;
};

// ── I. ADR-0027 §4: identity attributes ───────────────────────────────────
SECTIONS.I = async () => {
  await api('/api/attributes/devices/em1/identity', { method: 'DELETE' });
  await api('/api/attributes/settings', { method: 'PUT', body: JSON.stringify({ location_publish: 'coarse', publish_serials: false }) });
  const page = await open('/#/devices/em1');
  await page.waitForSelector('#deviceWsTabs');
  await openDeviceTab(page, 'attributes');
  await page.waitForSelector('#deviceDetailView .attr-add-sel');
  await page.selectOption('#deviceDetailView .attr-add-sel', 'identity');
  await page.click('#deviceDetailView [data-action="attrAdd"]');
  const f = n => `#deviceDetailView .attr-editor-host [data-attr-f="${n}"]`;
  await page.waitForSelector(f('description'));
  const names = await page.locator('#deviceDetailView .attr-editor-host [data-attr-f]').evaluateAll(els => els.map(e => e.dataset.attrF));
  check('I1 identity offers description, location, labels and the nameplate',
        ['description', 'location', 'labels', 'manufacturer', 'model', 'serial_number', 'firmware', 'installed_on'].every(n => names.includes(n)), names.join(','));
  check('I2 every identity field has a label', await page.evaluate(() =>
    [...document.querySelectorAll('#deviceDetailView .attr-editor-host [data-attr-f]')].every(el => document.querySelector(`label[for="${el.id}"]`))));
  await page.fill(f('description'), 'x'.repeat(501));
  const len = await page.evaluate(s => document.querySelector(s).value.length, f('description'));
  check('I3 a description cannot be longer than 500 characters (the field stops at 500)', len === 500
        && /500/.test(await describedBy(page, f('description'))), String(len));
  await page.fill(f('location'), 'garage, cabinet 2');
  await page.fill(f('serial_number'), 'SN-QA-1');
  await page.fill(f('description'), 'Main meter.');
  await page.fill(f('labels'), 'Bad Label');
  await page.click('#deviceDetailView [data-action="attrSave"]');
  await page.waitForFunction(() => /label/i.test(document.querySelector('#deviceDetailView .attr-err')?.textContent || ''));
  check('I4 a label that is not a slug is refused in words, announced', await page.getAttribute('#deviceDetailView .attr-err', 'role') === 'alert');
  await page.fill(f('labels'), 'qa, floor-1');
  await page.fill(f('installed_on'), '2024-05-17');
  await page.click('#deviceDetailView [data-action="attrSave"]');
  await page.waitForSelector('#deviceDetailView .attr-cats [data-attr-cat="identity"]');
  const held = (await api('/api/attributes/devices/em1')).body.attributes.identity || {};
  check('I5 identity saved', held.location === 'garage, cabinet 2' && (held.labels || []).join() === 'qa,floor-1' && held.serial_number === 'SN-QA-1', JSON.stringify(held));
  const bad = await api('/api/attributes/devices/em1/identity', { method: 'PUT', body: JSON.stringify({ installed_on: '2024-02-30' }) });
  check('I6 an impossible date is refused by the server', bad.status === 422);
  // atomic create with attributes
  const atomic = await api('/api/devices', { method: 'POST', body: JSON.stringify({ id: 'qa-attr', name: 'QA attr', template: 'eastron_sdm120', enabled: false,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15502, unit_id: 30 }, attributes: { identity: { labels: ['Not A Slug'] } } }) });
  check('I7 POST /api/devices with refused attributes creates nothing', atomic.status === 422 && (await api('/api/devices/qa-attr')).status === 404);

  // the privacy switches: location_publish and the serial numbers — visible settings, explained
  await page.evaluate(() => window.app.openSettingsTab('plant'));
  await page.waitForSelector('#attrPublish', { state: 'visible' });
  const card = await page.locator('.settings-card:has(#attrPublish)').innerText();
  check('I8 the coordinates switch says it also governs where a device is mounted', /mounted/.test(await describedBy(page, '#attrPublish')), card);
  check('I9 publishing serial numbers is a visible setting, explained (not API only)',
        await page.locator('#attrPublishSerials').count() === 1 && /signed-in users only/.test(await describedBy(page, '#attrPublishSerials'))
        && !(await page.isChecked('#attrPublishSerials')), card);

  if (!BROKER) { skip('I10-I14 meta: no QA_BROKER'); }
  else {
    await settle(page, 2500);
    let m = meta('mbg/devices/em1/meta');
    const id = (m.attributes || {}).identity || {};
    check('I10 meta carries type, type_label and attributes.identity', m.type === 'meter' && !!m.type_label && id.location === 'garage, cabinet 2'
          && (id.labels || []).join() === 'qa,floor-1', JSON.stringify(m));
    check('I11 the serial number is not published by default', !('serial_number' in id), JSON.stringify(id));
    await page.selectOption('#attrPublish', 'off');
    await settle(page, 2500);
    m = meta('mbg/devices/em1/meta');
    check('I12 "Not at all": where it is mounted leaves the meta', !('location' in ((m.attributes || {}).identity || {})), JSON.stringify(m.attributes));
    await page.waitForSelector('#attrPublish', { state: 'visible' });
    await page.selectOption('#attrPublish', 'coarse');
    await settle(page, 800);
    await page.focus('#attrPublishSerials');
    await page.keyboard.press('Space');
    await settle(page, 2500);
    m = meta('mbg/devices/em1/meta');
    check('I13 the serial switch on (from the keyboard): the serial number is published, focus kept',
          ((m.attributes || {}).identity || {}).serial_number === 'SN-QA-1' && await page.evaluate(() => document.activeElement?.id) === 'attrPublishSerials',
          JSON.stringify(m.attributes));
    await page.keyboard.press('Space');
    await settle(page, 800);
    check('I13b …and off again', (await api('/api/attributes')).body?.settings?.publish_serials === false);
    const m2 = meta('mbg/devices/em2/meta');
    // the meta's kind keeps the set it always published (a heat pump was null there): a user type never goes out as kind
    check('I14 a user type in meta: type = ours with its label; kind never the user id', m2.type === 'qa_chiller' && m2.type_label === 'QA chiller'
          && m2.kind !== 'qa_chiller', JSON.stringify(m2));
  }
  // ro
  await page.evaluate(() => window.app.setLanguage('ro'));
  await settle(page, 500);
  await page.evaluate(() => window.app.openDeviceDetail('em1'));
  await page.waitForSelector('#deviceWsTabs');
  await openDeviceTab(page, 'attributes');
  await page.waitForSelector('#deviceDetailView [data-attr-cat="identity"]');
  const roTxt = await page.locator('#deviceDetailView [data-attr-cat="identity"]').innerText();
  check('I15 ro: identity in Romanian', /Identitate|Descriere|Etichete|Unde/.test(roTxt), roTxt);
  await page.evaluate(() => window.app.setLanguage('en'));
  check('I16 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

// ── J. ADR-0029: the InfluxDB heartbeat ───────────────────────────────────
SECTIONS.J = async () => {
  check('J1 the file got the starting value 600', (await api('/api/config/influxdb')).body.heartbeat_interval === 600);
  const page = await open('/');
  await page.evaluate(() => window.app.openSettingsTab('influxdb'));
  await page.waitForSelector('#cfgInfluxHeartbeat', { state: 'visible' });
  await settle(page, 800);
  check('J2 Settings shows the stored 600, never a blank', await page.inputValue('#cfgInfluxHeartbeat') === '600');
  const hint = await describedBy(page, '#cfgInfluxHeartbeat');
  check('J3 its help: what it does, the trade-off, 0 = off', /last value/.test(hint) && /more points/.test(hint) && /0 = off/.test(hint), hint);
  check('J4 it sits next to the write interval', await page.evaluate(() => {
    const a = document.getElementById('cfgInfluxHeartbeat'), b = document.getElementById('cfgInfluxWriteInterval');
    return !!a && !!b && a.closest('.settings-card') === b.closest('.settings-card');
  }));
  await page.evaluate(() => window.app.openDeviceDetail('em1'));
  await page.waitForSelector('#deviceWsTabs');
  await openDeviceTab(page, 'outputs');
  await page.click('#outTab-influxdb');
  await page.waitForSelector('#pubHb-influxdb');
  check('J5 Outputs → InfluxDB: empty = the gateway\'s, "gateway: 600 s" beside it', await page.inputValue('#pubHb-influxdb') === ''
        && (await page.locator('#pubHbGw-influxdb').innerText()).trim() === 'gateway: 600 s');
  check('J6 …with the same label as Settings', /Rewrite an unchanged value every/.test(await page.locator('label[for="pubHb-influxdb"]').innerText()));
  await page.evaluate(() => window.app.setLanguage('ro'));
  await settle(page, 500);
  await page.evaluate(() => window.app.openSettingsTab('influxdb'));
  await page.waitForSelector('#cfgInfluxHeartbeat', { state: 'visible' });
  check('J7 ro: label and help in Romanian', /Rescrie o valoare neschimbată/.test(await page.locator('label[for="cfgInfluxHeartbeat"]').innerText())
        && /0 = oprit/.test(await describedBy(page, '#cfgInfluxHeartbeat')));
  await page.evaluate(() => window.app.setLanguage('en'));
  check('J8 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

// ── K. the removed switches leave no trace ────────────────────────────────
SECTIONS.K = async () => {
  const st = (await api('/api/status')).body;
  check('K1 /api/status no longer reports rules_recheck / raw_lanes', !('rules_recheck' in st) && !('raw_lanes' in st));
  const en = JSON.parse(readFileSync(join(REPO, 'ui/languages/en.json'), 'utf8'));
  const ro = JSON.parse(readFileSync(join(REPO, 'ui/languages/ro.json'), 'utf8'));
  check('K2 their i18n keys are gone', !['status.why.recheckOff', 'status.why.rawLanesOff'].some(k => k in en || k in ro));
  const page = await open('/#/status');
  await settle(page, 1500);
  const pages = [];
  for (const p of ['status', 'settings', 'rules']) {
    await page.evaluate(p => window.app.navigateTo(p), p);
    await settle(page, 900);
    pages.push(await page.locator('body').innerText());
  }
  for (const tab of ['general', 'mqtt', 'influxdb', 'security']) {
    await page.evaluate(t => window.app.openSettingsTab(t), tab).catch(() => {});
    await settle(page, 500);
    pages.push(await page.locator('body').innerText());
  }
  const all = pages.join('\n');
  check('K3 no page mentions the recheck or raw-lanes switch', !/recheck|raw lanes|raw_lanes|MBG_RULES_RECHECK|MBG_RAW_LANES|switched off/i.test(all),
        (all.match(/.{0,60}(recheck|raw lanes|switched off).{0,60}/i) || [''])[0]);
  await page.context().close();
};

// ── L. ADR-0025 §6: Home Assistant discovery follows the MQTT output ──────
SECTIONS.L = async () => {
  if (!BROKER) { skip('L: no QA_BROKER'); return; }
  const before = retainedCount('homeassistant/+/mbg_dev_em1/+/config');
  check('L1 em1 (MQTT on) has Home Assistant discovery entries', before > 0, String(before));
  const page = await open('/#/devices/em1');
  await page.waitForSelector('#deviceWsTabs');
  await openDeviceTab(page, 'outputs');
  await page.click('#outTab-mqtt');
  await settle(page);
  // the switch is staged in the Edit modal (L3.4): it applies on Save
  const sw = page.locator('#outDetail #outEnabled');
  await sw.uncheck();
  await saveDeviceEditor(page);                      // MQTT off: reviewed, answered yes
  await settle(page, 3000);
  const after = retainedCount('homeassistant/+/mbg_dev_em1/+/config');
  check('L2 MQTT output off: its discovery entries are taken back', after === 0, `${before} → ${after}`);
  await openDeviceTab(page, 'outputs');
  await page.click('#outTab-mqtt');
  await page.locator('#outDetail #outEnabled').check();
  await saveDeviceEditor(page);
  await settle(page, 1500);
  await page.context().close();
};

// ── M. every string in en and ro ──────────────────────────────────────────
SECTIONS.M = async () => {
  const en = JSON.parse(readFileSync(join(REPO, 'ui/languages/en.json'), 'utf8'));
  const ro = JSON.parse(readFileSync(join(REPO, 'ui/languages/ro.json'), 'utf8'));
  // Romanian has a third plural form (.few): a key only it holds is that
  const onlyEn = Object.keys(en).filter(k => !(k in ro));
  const onlyRo = Object.keys(ro).filter(k => !(k in en) && !/\.few$/.test(k));
  check('M1 en.json and ro.json hold the same keys', !onlyEn.length && !onlyRo.length, `en only: ${onlyEn.slice(0, 8)} ro only: ${onlyRo.slice(0, 8)}`);
  // every key the code asks for exists in both (a missing one falls back to English in ro)
  const used = new Set();
  for (const f of readdirSync(join(REPO, 'ui/js')).filter(f => f.endsWith('.js'))) {
    const src = readFileSync(join(REPO, 'ui/js', f), 'utf8');
    for (const m of src.matchAll(/\bt[n]?\(\s*'([a-zA-Z][\w-]*\.[\w.-]+)'/g)) used.add(m[1]);
  }
  const html = readFileSync(join(REPO, 'ui/templates/index.html'), 'utf8');
  for (const m of html.matchAll(/data-i18n(?:-[a-z]+)?="([\w.-]+)"/g)) used.add(m[1]);
  const has = (dict, k) => k in dict || `${k}.one` in dict || `${k}.other` in dict;
  const missing = [...used].filter(k => !/[._]$/.test(k) && (!has(en, k) || !has(ro, k)));
  check('M2 every key the UI asks for is in en.json and ro.json', !missing.length, missing.slice(0, 20).join(', '));
  // the new areas: the Romanian is Romanian (not the English left in)
  const areas = /^(attr\.publish|attr\.serials|devices\.wizard\.bus|bridges\.tapped|identity|types|devTypes|serialPorts|cf\.port|influx\.heartbeat|attr\.identity|attr\.cat\.identity|wizMtr\.type|tpl\.kind|devices\.writeLock|write\.locked)/;
  const same = Object.keys(en).filter(k => areas.test(k) && ro[k] === en[k] && /[a-z]{4,}\s[a-z]{3,}/i.test(en[k]));
  check('M3 the new strings are translated (ro differs from en)', !same.length, same.slice(0, 12).join(', '));
};

try {
  for (const [k, fn] of Object.entries(SECTIONS)) {
    if (ONLY.length && !ONLY.includes(k)) continue;
    console.log(`\n── ${k} ──`);
    try { await fn(); } catch (e) { check(`${k} ran to the end`, false, e && e.stack || e); }
  }
} finally {
  // leave the gateway as it started
  await api('/api/devices/qa-new', { method: 'DELETE' }).catch(() => {});
  await api('/api/devices/qa-api', { method: 'DELETE' }).catch(() => {});
  await api('/api/devices/qa-scoped', { method: 'DELETE' }).catch(() => {});
  await api('/api/devices/em2/organise', { method: 'PATCH', body: JSON.stringify({ type: null }) }).catch(() => {});
  await api('/api/attributes/devices/em1/identity', { method: 'DELETE' }).catch(() => {});
  await api('/api/attributes/devices/em2/identity', { method: 'DELETE' }).catch(() => {});
  for (const id of ['qa_chiller', 'qa_boiler', 'qa_pump']) await api(`/api/device-types/${id}`, { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
