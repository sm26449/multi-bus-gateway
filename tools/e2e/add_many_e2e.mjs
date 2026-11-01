// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// "Many of the same on one bus" in the new Add (ADR-0031 §2, L3.7):
//
// A  Step 1 → Many of the same: the connection step, its address question
//    holding the scan panel; Scan aria-disabled until the bus is said
// B  the bridge bus :14197 (polled by brpoll, unit 4): Master pre-filled;
//    Scan this bus → 4 units, unit 4 "already: brpoll" (not tickable), one
//    socket — the converter accepted no second client (the scan rode
//    brpoll's connection); Listen for addresses aria-disabled with its reason
// C  tick 1, 2, 3 → Add 3 selected → Units: ids sdm120-u1…u3, names, model;
//    an id edited; a duplicate id refused before anything is sent
// D  Create 3: one dry run per row, then the creates — each row "created";
//    the devices exist, on the bridge bus, their units, write-locked
// E  a tapped bus (:14196, brtap listens; a foreign master polls units 1, 2):
//    Listen-only pre-filled, Scan aria-disabled with the ADR-0025 reason,
//    Listen for addresses → units 1 and 2 heard, unit 2 "already: brtap",
//    nothing written to the bus
// F  Romanian: the panel's words; no page errors
// G  an armed rule on the bus (the server's 409 played by page.route): the
//    app dialog names the rule and the delay; Scan anyway sends confirm_slow
//
// Against a throwaway gateway of the qa fixture with the bridge sims inside it:
//   MBG_FX_NAME=mbg-fx-l37 tools/e2e/fixture_up.sh qa 18371 --login e2e-pass
//   docker cp tools/e2e/sims/bridge_sims.py mbg-fx-l37:/tmp/
//   docker exec -d -u 10001 mbg-fx-l37 sh -c 'python3 /tmp/bridge_sims.py transparent 14197 1,2,3,4 > /tmp/br_14197.log 2>&1'
//   docker exec -d -u 10001 mbg-fx-l37 sh -c 'python3 /tmp/bridge_sims.py master 14196 > /tmp/br_14196.log 2>&1'
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18371 MBG_PASS=e2e-pass MBG_FX_NAME=mbg-fx-l37 node tools/e2e/add_many_e2e.mjs
//   docker rm -f mbg-fx-l37
// It creates sdm120-u1, sdm120-u2, e2e-many-3 and deletes them.
import { execFileSync } from 'child_process';
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18371';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const FX = process.env.MBG_FX_NAME || 'mbg-fx-l37';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)).slice(0, 500) : ''}`);
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const simStats = port => {
  try { return JSON.parse(execFileSync('docker', ['exec', FX, 'cat', `/tmp/br_sim_${port}.json`]).toString()); } catch (e) { return null; }
};

const api = await pwRequest.newContext({ baseURL: BASE });
await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const MADE = ['sdm120-u1', 'sdm120-u2', 'sdm120-u3', 'e2e-many-3'];
for (const id of MADE) await api.delete(`/api/devices/${id}`);

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|409|412|422/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.sent = [];
  page.on('request', r => { if (r.method() !== 'GET') page.sent.push({ method: r.method(), url: r.url(), body: r.postData() }); });
  await page.addInitScript(o => { try { if (o.lang) localStorage.setItem('mbg-lang', o.lang); } catch (e) { /* */ } }, { lang: opts.lang || '' });
  await page.goto(BASE + '/?newadd=1#/devices');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.waitForSelector('#devicesPage [data-action="openDeviceWizard"]', { state: 'visible' });
  return page;
}

// Step 1: the model, then "Many of the same"; the bridge bus chosen
async function toMany(page, busPort) {
  await page.click('#devicesPage [data-action="openDeviceWizard"]');
  await page.waitForSelector('#addDeviceModal.active #adResults [role="option"]');
  await page.fill('#adQ', 'SDM120');
  await page.waitForTimeout(200);
  await page.click('#adResults [data-ad-tpl="eastron_sdm120"]');
  await page.click('[data-ad-way="many"]');
  await page.waitForSelector('#adManyConn #adManyPortKind-serial', { state: 'attached' });
  await page.waitForTimeout(400);
  await page.click('label:has(#adManyPortKind-serial)');
  await page.click('label:has(#adManyReach-bridge)');
  await page.waitForSelector('#adManyBus');
  await page.selectOption('#adManyBus', `panel::${busPort}`);
  await page.waitForTimeout(500);
}

const state1 = async (page) => page.locator('#adManyState').innerText().catch(() => '');
async function waitScan(page, re, ms = 30000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const s = await state1(page);
    if (re.test(s)) return s;
    await sleep(300);
  }
  return state1(page);
}

try {
  // ── A, B, C, D ─────────────────────────────────────────────────────
  {
    const page = await open();
    await page.click('#devicesPage [data-action="openDeviceWizard"]');
    await page.waitForSelector('#addDeviceModal.active #adResults [role="option"]');
    await page.click('[data-ad-way="many"]');
    await page.waitForSelector('#adManyConn #adManyPortKind-serial', { state: 'attached' });
    await page.waitForTimeout(400);
    const rail = await page.$$eval('#addDevRail li .dev-shell-steplbl', els => els.map(e => e.textContent.trim()));
    check('A Many of the same: two steps — the connection with its scan, then Units', rail.length === 2 && /Units/.test(rail[1]), rail);
    check('A the scan panel sits in the connection step\'s address question… once the bus is said',
          (await page.locator('#adManyAddrQ #adManyPanel').count()) === 0 || (await page.getAttribute('#adMany-poll', 'aria-disabled')) === 'true');
    check('A Next is aria-disabled until units are ticked', (await page.getAttribute('#addDevNext', 'aria-disabled')) === 'true');
    await page.evaluate(() => window.app.closeModal('addDeviceModal'));
    await page.waitForTimeout(200);
    if (await page.locator('#appDialog.active').count()) await page.click('#appDialog [data-dialog-ok], #appDialog .btn-danger').catch(() => {});

    const s0 = simStats(14197);
    await toMany(page, 14197);
    check('B the bus polled by brpoll: Master pre-filled', await page.isChecked('#adManyRole-poll'));
    check('B the panel is in the address question', await page.locator('#adManyAddrQ #adManyPanel').count() === 1);
    check('B Listen for addresses aria-disabled on a bus the gateway asks, its reason given',
          (await page.getAttribute('#adMany-listen', 'aria-disabled')) === 'true' && /master/i.test(await page.getAttribute('#adMany-listen', 'data-am-why') || ''));
    await page.fill('#adManyTo', '5').catch(() => {});
    await page.locator('#adManyPanel details.am-range summary').click().catch(() => {});
    await page.fill('#adManyTo', '5');
    await page.click('#adMany-poll');
    const done = await waitScan(page, /Done|failed|refused|409/i);
    check('B the scan ends: 3 units answered (unit 4, brpoll\'s address, is not asked)', /3 unit/.test(done), done);
    const rows = await page.$$eval('#adManyTable tbody tr', trs => trs.map(tr => tr.innerText.replace(/\s+/g, ' ').trim()));
    check('B a row per unit, unit 4 already brpoll — not asked', rows.length === 4 && /already: brpoll/i.test(rows[3]) && /not asked/.test(rows[3]), rows);
    check('B unit 4 is not tickable (aria-disabled)', (await page.getAttribute('#adManyU-4', 'aria-disabled')) === 'true');
    const s1 = simStats(14197);
    check('B one socket: the converter took no second client (the scan rode brpoll\'s connection)',
          s0 && s1 && s1.accepts === s0.accepts, { s0, s1 });
    const posted = page.sent.filter(x => /\/api\/bus-scan$/.test(x.url)).map(x => JSON.parse(x.body || '{}'));
    check('B a shared scan says how much it slows the bus (its devices\' reads may wait)',
          /may wait up to/.test(await page.locator('#adManyPanel').innerText()));
    check('B the scan asked: mode poll, units 1–5, the bus by its connection', posted.length === 1 && posted[0].mode === 'poll'
          && posted[0].from === 1 && posted[0].to === 5 && posted[0].connection.bridge === 'panel' && posted[0].connection.unit_id === undefined, posted);
    await page.click('#adManyU-4', { force: true });
    check('B a click on unit 4 ticks nothing', !(await page.isChecked('#adManyU-4')));
    for (const u of [1, 2, 3]) await page.click(`#adManyU-${u}`);
    check('C Add 3 selected', /Add 3 selected/.test(await page.locator('#adManyGo').innerText()));
    await page.click('#adManyGo');
    await page.waitForSelector('#adUnitsTable');
    const ids = await page.$$eval('#adUnitsTable [data-am-field="id"]', els => els.map(e => e.value));
    check('C Units: ids {model}-u{unit}', JSON.stringify(ids) === JSON.stringify(['sdm120-u1', 'sdm120-u2', 'sdm120-u3']), ids);
    const tpls = await page.$$eval('#adUnitsTable [data-am-field="template"]', els => els.map(e => e.value));
    check('C Units: the model chosen in Step 1 on every row', tpls.every(x => x === 'eastron_sdm120'), tpls);
    check('C Next says Create 3 devices', /Create 3 devices/.test(await page.locator('#addDevNext').innerText()));
    // a duplicate id is refused before anything is sent
    await page.fill('#adUId-2', 'sdm120-u2');
    const before = page.sent.length;
    await page.click('#addDevNext');
    await page.waitForTimeout(400);
    check('C a duplicate id: said at the row, nothing sent', page.sent.length === before
          && /Unit 3/.test(await page.locator('#addDevErrors').innerText().catch(() => '')), page.sent.slice(before));
    await page.fill('#adUId-2', 'e2e-many-3');
    await page.click('#addDevNext');
    const end = Date.now() + 20000;
    while (Date.now() < end && (await page.$$eval('#adUnitsTable tbody tr', trs => trs.filter(tr => tr.dataset.amStatus === 'created').length)) < 3) await sleep(300);
    const st = await page.$$eval('#adUnitsTable tbody tr', trs => trs.map(tr => tr.dataset.amStatus));
    check('D every row created', st.every(x => x === 'created'), st);
    const sentD = page.sent.filter(x => /\/api\/devices(\?dry_run=true)?$/.test(x.url));
    const dry = sentD.filter(x => /dry_run/.test(x.url)).length, real = sentD.filter(x => !/dry_run/.test(x.url)).length;
    check('D one dry run per row first, then one create per row', dry === 3 && real === 3
          && sentD.findIndex(x => !/dry_run/.test(x.url)) === 3, sentD.map(x => x.url));
    for (const [id, u] of [['sdm120-u1', 1], ['sdm120-u2', 2], ['e2e-many-3', 3]]) {
      const r = await api.get(`/api/devices/${id}`);
      const d = r.ok() ? (await r.json()).device : null;
      check(`D ${id}: exists on the bridge bus, unit ${u}, write-locked`, d && d.bridge === 'panel'
            && +d.bridge_port === 14197 && +d.connection.unit_id === u && d.write_locked === true, d && d.connection);
    }
    check('D Next says Done', /Done/.test(await page.locator('#addDevNext').innerText()));
    await page.click('#addDevNext');
    await page.waitForTimeout(300);
    check('D Done closes the dialog', !(await page.locator('#addDeviceModal.active').count()));
    check('A–D no page errors', page.errs.length === 0, page.errs);
    await page.context().close();
  }

  // ── E: a tapped bus ────────────────────────────────────────────────
  {
    const page = await open();
    const w0 = simStats(14196);
    await toMany(page, 14196);
    check('E the bus brtap listens to: Listen-only pre-filled', await page.isChecked('#adManyRole-listen'));
    check('E Scan this bus aria-disabled, the ADR-0025 reason given', (await page.getAttribute('#adMany-poll', 'aria-disabled')) === 'true'
          && /only listened to/.test(await page.getAttribute('#adMany-poll', 'data-am-why') || ''));
    const sentBefore = page.sent.length;
    await page.click('#adMany-poll', { force: true });
    await page.waitForTimeout(300);
    check('E a click on the refused Scan sends nothing', !page.sent.slice(sentBefore).some(x => /bus-scan/.test(x.url)));
    await page.click('#adMany-listen');
    const done = await waitScan(page, /Done|failed|refused/i, 30000);
    check('E Listen for addresses: units 1 and 2 heard', /2 address/.test(done), done);
    const rows = await page.$$eval('#adManyTable tbody tr', trs => trs.map(tr => tr.innerText.replace(/\s+/g, ' ').trim()));
    check('E the rows: what the master asked; unit 2 already brtap', rows.length === 2 && /FC3 @0 x2/.test(rows[0]) && /already: brtap/i.test(rows[1]), rows);
    const w1 = simStats(14196);
    check('E nothing was written to the listened bus', w1 && w1.written === (w0 ? w0.written : 0), { w0, w1 });
    const posted = page.sent.filter(x => /\/api\/bus-scan$/.test(x.url)).map(x => JSON.parse(x.body || '{}'));
    check('E the listen asked mode listen', posted.length === 1 && posted[0].mode === 'listen', posted);
    check('E no page errors', page.errs.length === 0, page.errs);
    await page.context().close();
  }

  // ── G: an armed rule depends on the bus ────────────────────────────
  {
    const page = await open();
    let calls = 0;
    await page.route('**/api/bus-scan', async route => {
      calls++;
      const body = JSON.parse(route.request().postData() || '{}');
      if (!body.confirm_slow) {
        return route.fulfill({ status: 409, json: { detail: {
          errors: ['… Armed rule(s) ov-u1 read or drive a device on this bus …'], rules: ['ov-u1'],
          slow: { devices: ['brpoll'], reads_per_cycle: 2, per_read_s: 1.3, worst_per_cycle_s: 2.6, rules: ['ov-u1'] } } } });
      }
      return route.continue();
    });
    await toMany(page, 14197);
    await page.locator('#adManyPanel details.am-range summary').click().catch(() => {});
    await page.fill('#adManyTo', '5');
    page.dlg.answers.push(true);
    await page.click('#adMany-poll');
    const done = await waitScan(page, /Done|failed|refused/i);
    const asked = page.dlg.seen.find(x => /armed rules/i.test(x.title || ''));
    check('G the app dialog names the rule and the delay', asked && /ov-u1/.test(asked.message) && /1\.3 s/.test(asked.message)
          && /2\.6 s/.test(asked.message), page.dlg.seen);
    const posted = page.sent.filter(x => /\/api\/bus-scan$/.test(x.url)).map(x => JSON.parse(x.body || '{}'));
    check('G Scan anyway sends confirm_slow, the first ask did not', calls === 2 && !posted[0].confirm_slow && posted[1].confirm_slow === true, posted);
    check('G the scan then runs', /Done/.test(done), done);
    check('G no page errors', page.errs.length === 0, page.errs);
    await page.context().close();
  }

  // ── F: Romanian ────────────────────────────────────────────────────
  {
    const page = await open({ lang: 'ro' });
    await toMany(page, 14197);
    check('F ro: the panel speaks Romanian', /Scanează magistrala/.test(await page.locator('#adManyPanel').innerText())
          && /Ascultă adresele/.test(await page.locator('#adManyPanel').innerText()));
    check('F ro: no page errors', page.errs.length === 0, page.errs);
    await page.context().close();
  }
} finally {
  for (const id of MADE) await api.delete(`/api/devices/${id}`);
  await browser.close();
}
const bad = results.filter(r => !r.ok);
console.log(`\n${results.length - bad.length}/${results.length} passed`);
process.exit(bad.length ? 1 : 0);
