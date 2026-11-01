// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A CAN device added and edited through the UI, as an
// operator meets it: CAN on Add Device's Connection step (L3.9, By
// connection) with its fields and defaults, the listen-only role said, Test
// saying plainly that the interface is missing (a throwaway gateway in a
// container has no CAN interface — as the production host), the listen-only
// hard gate (refused, then passed on a FAKED listen-only can0), CAN models
// only on the model step, Create, the list row, the device page's Connection tab
// (one edited field sent, CAN templates only), no sideways scroll on a phone.
//
//   tools/e2e/fixture_up.sh safety 18133
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18133 OUT=<dir> node tools/e2e/can_device_form_e2e.mjs
//   docker rm -f mbg-fx-safety
import { chromium } from 'playwright';
import { openDeviceTab, saveDeviceEditor } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18133';
const OUT = process.env.OUT || '';
const TPL = 'pylontech_can_inverter';
const ID = 'e2e-bank-can';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }, scheme = 'light') {
  const page = await browser.newPage({ viewport, colorScheme: scheme });
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
const shot = async (page, name) => { if (OUT) await page.screenshot({ path: `${OUT}/can-${name}.png` }); };
const noSideways = page => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);

try {
  // a device left by an earlier run goes first
  await fetch(`${BASE}/api/devices/${ID}`, { method: 'DELETE' }).catch(() => {});
  const page = await open();

  // ── Add Device (L3.9): By connection, CAN — its fields and defaults ──
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
  await page.click('[data-ad-way="connection"]');
  await page.waitForSelector('#csAddPortKind-can', { state: 'attached' });
  const canSeg = page.locator('label:has(#csAddPortKind-can)');
  check('CAN is offered among the ports', await canSeg.count() === 1);
  await canSeg.click();
  await page.waitForSelector('#csAddCanFields');
  check('interface defaults to can0', await page.inputValue('#csAddCanChannel') === 'can0');
  check('bitrate defaults to 500 kbit/s', await page.inputValue('#csAddCanBitrate') === '500000');
  check('bitrate is labelled as set by the host',
    /set by the host/i.test(await page.locator('label[for="csAddCanBitrate"]').innerText()));
  check('stale after defaults to 5 s', await page.inputValue('#csAddCanStale') === '5');
  const note = await page.locator('#csAddCanFields').innerText();
  check('the role is said: listen-only, never transmits', /never transmits/.test(note), note);
  check('no Modbus field is left on the CAN form',
    !(await page.locator('#adConnHost #csAddHost, #adConnHost #csAddUnit, #adConnHost #csAddSerial').count()));
  await shot(page, 'add-can-connection');

  // ── what the form refuses before asking ──
  await page.fill('#csAddCanChannel', '');
  await page.click('#addDevNext');
  await page.waitForSelector('#csAddCanChannel[aria-invalid="true"]');
  check('an empty interface is marked at its field', true);
  await page.fill('#csAddCanChannel', 'can0');
  await page.locator('#csAddCanChannel').dispatchEvent('change');
  await page.waitForTimeout(600);

  // ── listen-only is a hard gate: can0 is not on this host ──
  await page.click('#addDevNext');
  await page.waitForTimeout(600);
  check('the gate keeps Next on Connection, with the ip link command',
    await page.evaluate(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep) === 'connection'
    && /ip link set can0 .*listen-only on/.test(await page.locator('#csAddCanGate').innerText()));
  // the host FAKED to have can0 up and listen-only: the gate passes
  await page.route('**/api/can-interfaces', r => r.fulfill({ json: { interfaces: [
    { name: 'can0', kind: 'can', up: true, bitrate: 500000, listen_only: true, devices: [] }] } }));
  await page.click('#csAddCsRefresh');
  await page.waitForTimeout(700);
  check('can0 up and listen-only: the gate passes', (await page.locator('#csAddCanGate.ok').count()) === 1);
  await shot(page, 'add-can-gate-ok');

  // ── Test: past the gate (the page was told can0 is up), the gateway itself
  // has no can0 — said plainly ──
  const out = page.locator('#csAddTestResult');
  const nTest = page.reqs.length;
  await page.click('#csAddTestBtn');
  await page.waitForFunction(() => document.getElementById('csAddTestResult')?.textContent.trim()
    && !/…/.test(document.getElementById('csAddTestBtn')?.textContent || ''), null, { timeout: 20000 });
  const said = (await out.textContent()).trim();
  check('Test says the CAN interface is missing', /no CAN interface 'can0'/.test(said), said);
  const testReq = page.reqs.slice(nTest).find(r => r.url.endsWith('/api/devices/test'));
  const testBody = JSON.parse(testReq?.body || '{}');
  check('Test sends the CAN connection', testBody.connection?.protocol === 'can' && testBody.connection?.channel === 'can0',
    testReq?.body);


  // ── the model second: CAN models only ──
  await page.click('#addDevNext');
  await page.waitForFunction(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep === 'what', null, { timeout: 8000 });
  const offered = await page.locator('#adResults [data-ad-tpl]').evaluateAll(els => els.map(e => e.dataset.adTpl));
  check('the model step offers the CAN model', offered.includes(TPL), offered.join(','));
  const tpls = (await (await fetch(`${BASE}/api/device-templates`)).json()).templates || [];
  const canIds = new Set(tpls.filter(x => x.transport === 'can').map(x => x.id));
  check('…and CAN models only', offered.length && offered.every(x => canIds.has(x)), offered.join(','));
  await page.click(`#adResults [data-ad-tpl="${TPL}"]`);
  await page.click('#addDevNext');
  // the check: the gateway itself has no can0 (only the page was told so)
  await page.waitForFunction(() => document.querySelector('#adCheck .pv-refused, #adCheck .pv-status:not(.is-running)'), null, { timeout: 25000 }).catch(() => {});
  check('the check cannot read can0 here, and Next goes on without it', /without a check/.test(await page.locator('#addDevNext').innerText()));
  await page.click('#addDevNext');

  // ── Name & save and Create ──
  await page.waitForSelector('#adName');
  await page.fill('#adName', 'Bank (CAN)');
  await page.fill('#adId', ID);
  await page.locator('#adId').dispatchEvent('input');
  await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 });
  const nCreate = page.reqs.length;
  await page.click('#addDevNext');
  await page.waitForSelector('#adResult', { timeout: 20000 });
  const create = page.reqs.slice(nCreate).find(r => r.method === 'POST' && /\/api\/devices$/.test(r.url));
  const body = JSON.parse(create?.body || '{}');
  check('Create sends the CAN connection', JSON.stringify(body.connection) === JSON.stringify(
    { protocol: 'can', carrier: 'socketcan', channel: 'can0', bitrate: 500000, stale_after_s: 5 }), create?.body);
  check('Create sends the CAN template', body.template === TPL, body.template);
  await page.click('#adResDone');
  await page.unroute('**/api/can-interfaces');
  const dev = ((await (await fetch(`${BASE}/api/devices`)).json()).devices || []).find(d => d.id === ID);
  check('the device exists as CAN', dev?.protocol === 'can' && dev?.connection?.channel === 'can0', JSON.stringify(dev?.connection));

  // ── the list row ──
  await page.click('[data-page="devices"]');
  if (!(await page.isVisible('#devicesListView')))
    await page.click('#deviceRegistersView [data-action="closeDeviceRegisters"]');
  await page.waitForSelector('#devicesListView', { state: 'visible' });
  const row = page.locator(`.device-row[data-f="${ID}"]`);
  await row.waitFor({ state: 'visible' });
  const sub = await row.locator('.device-row-sub').innerText();
  check('the list row says CAN on can0 at 500 kbit/s', /CAN · can0 · 500 kbit\/s/.test(sub), sub);
  check('the list row has no unit', !/unit \d/.test(sub), sub);
  if (dev?.enabled !== false) {
    const stats = await row.locator('.device-row-stats').innerText();
    check('the list row shows the CAN counters', /0 frames · 0 unknown \(0 ids\) · 0 decode errors/.test(stats)
      && /No such device/.test(stats), stats);
    await shot(page, 'device-list');
  }

  // ── the device page: its Test and its Connection tab ──
  await page.evaluate(i => window.app.openDeviceDetail(i), ID);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvCanFields');
  check('the device page opens the CAN form with its values',
    await page.inputValue('#ddvCanChannel') === 'can0' && await page.inputValue('#ddvCanStale') === '5');
  check('other connection kinds are locked on a saved CAN device',
    await page.locator('#ddvConn input[name="ddvPortKind"][value="ethernet"][aria-disabled="true"]').count() === 1);
  // the template choice: the Edit modal's Model section
  const tplOpts = await page.locator('#ddvTemplate option').evaluateAll(els => els.map(e => e.value).filter(Boolean));
  check('the device page offers CAN templates only', tplOpts.length && tplOpts.every(x => canIds.has(x)), tplOpts.join(','));
  await shot(page, 'device-connection');
  // listen-only is a hard gate (L3.6): can1 is not on this host — refused,
  // with the ip link command; then the host is FAKED to have can1 up and
  // listen-only, and the edit goes through
  await page.fill('#ddvCanChannel', 'can1');
  await page.locator('#ddvCanChannel').dispatchEvent('change');
  await page.waitForTimeout(900);
  check('an interface this host has not: the gate refuses it, with the command',
    (await page.getAttribute('#devEditSave', 'aria-disabled')) === 'true'
    && /ip link set can1 .*listen-only on/.test(await page.locator('#ddvCanGate').innerText()));
  await page.route('**/api/can-interfaces', r => r.fulfill({ json: { interfaces: [
    { name: 'can0', kind: 'can', up: true, bitrate: 500000, listen_only: true, devices: [ID] },
    { name: 'can1', kind: 'can', up: true, bitrate: 500000, listen_only: true, devices: [] }] } }));
  await page.evaluate(() => window.app._csRefresh('ddv'));
  await page.waitForTimeout(600);
  check('can1 up and listen-only: the gate passes', /listen-only/.test(await page.locator('#ddvCanGate').innerText())
    && (await page.locator('#ddvCanGate.ok').count()) === 1);
  const nPut = page.reqs.length;
  await saveDeviceEditor(page);
  const put = page.reqs.slice(nPut).find(r => r.method === 'PATCH' && r.url.endsWith(`/api/devices/${ID}`));
  const pbody = JSON.parse(put?.body || '{}').sections || {};
  check('an edit sends the changed field and its protocol',
    JSON.stringify(pbody.connection) === JSON.stringify({ channel: 'can1', protocol: 'can' }), put?.body);
  const after = ((await (await fetch(`${BASE}/api/devices`)).json()).devices || []).find(d => d.id === ID);
  check('the edit is kept, the rest stays', after?.connection?.channel === 'can1' && after?.connection?.bitrate === 500000
    && after?.connection?.stale_after_s === 5, JSON.stringify(after?.connection));
  const saved = await (await fetch(`${BASE}/api/devices/${ID}/test`, { method: 'POST' })).json();
  check('the saved device’s Test says can1 is missing', saved.ok === false && /no CAN interface 'can1'/.test(saved.message), saved.message);
  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── a phone, dark: the CAN form fits ──
  const p = await open({ width: 390, height: 844 }, 'dark');
  await p.evaluate(() => window.app.openDeviceWizard());
  await p.waitForSelector('#addDeviceModal.active');
  await p.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
  await p.click('[data-ad-way="connection"]');
  await p.locator('label:has(#csAddPortKind-can)').click();
  await p.waitForSelector('#csAddCanFields');
  check('phone: no sideways scroll on the CAN form', await noSideways(p));
  await shot(p, 'add-can-phone-dark');
  await p.close();
} catch (e) {
  check('the run finished', false, String(e && e.stack || e));
} finally {
  await fetch(`${BASE}/api/devices/${ID}`, { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
const bad = results.filter(r => !r.ok).length;
console.log(`\n${results.length - bad}/${results.length} passed`);
process.exit(bad ? 1 : 0);
