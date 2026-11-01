// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Add Device's first step and the measured path (ADR-0031 §2; L3.5, the
// only Add since L3.9 — the flag and the old wizard are gone):
// A  the Add button opens the new flow, never the old wizard (no flag)
// B  the dialog — the search focused, the step rail an <ol> with
//    aria-current="step", Back hidden, Next aria-disabled with its reason said
// C  search: a model, a brand, a Romanian synonym; the count said
// D  the type chips: a radiogroup — the arrows choose, the list follows
// E  the models: a listbox — ArrowDown from the search, Space picks, Enter
//    goes on; the step's heading takes the focus; Back keeps the search and
//    the pick; the rail's done step goes back too
// F  the ways: By connection swaps the first two steps; "Choose a model
//    instead" comes back with everything kept; Many of the same opens the
//    connection step with its scan (L3.7; add_many_e2e.mjs drives it)
// G  the dirty guard: Esc asks; Keep editing keeps
// H  the measured path, end to end: a UPS measured by two meters (input em1,
//    output em2) — the POST byte-identical to the old wizard's; the device
//    exists with both links; Back to Meters keeps the rows
// I  Found (n): what /api/mqtt-discovery holds; Use is aria-disabled, with
//    its reason, when the device's model is not on this gateway
// J  a model picked: the Connection step itself (no hand-over any more)
// K  Romanian
// L  a phone (390 px): full screen, the rail one line, nothing scrolls sideways
// M  the model decides the port: an MQTT and a CAN model open the Connection
//    step on that port, said as a fact with Change model (review M1)
// N  the dirty guard: a search alone is nothing to lose; words in + Add
//    type… are (review m6)
// O  the dialog opens at once, busy while the catalog loads; a failed load
//    is said in words (review m7); Found's disabled Use looks disabled (m3)
//
// Against a throwaway gateway of the qa fixture, login on:
//   MBG_FX_NAME=mbg-fx-l35add tools/e2e/fixture_up.sh qa 18351 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18351 MBG_PASS=e2e-pass node tools/e2e/add_device_new_e2e.mjs
//   docker rm -f mbg-fx-l35add
// It creates e2e-ups-pair and deletes it again.
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18351';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : ''}`);
};

const api = await pwRequest.newContext({ baseURL: BASE });
await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const NEW_ID = 'e2e-ups-pair';
await api.delete(`/api/devices/${NEW_ID}`);

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const contexts = [];
async function open(opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  contexts.push(ctx);
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|412|422/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  await page.addInitScript(o => {
    try {
      if (o.lang) localStorage.setItem('mbg-lang', o.lang);
    } catch (e) { /* */ }
  }, { lang: opts.lang || '' });
  if (opts.route) await opts.route(page);
  await page.goto(BASE + '/#/devices');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.waitForSelector('#devicesPage [data-action="openDeviceWizard"]', { state: 'visible' });
  return page;
}
const isOpen = (page, id) => page.locator(`#${id}.active`).count().then(n => n === 1);
const active = page => page.evaluate(() => document.activeElement?.id || '');
async function openAdd(page) {
  await page.click('#devicesPage [data-action="openDeviceWizard"]');
  await page.waitForSelector('#addDeviceModal.active #adResults [role="option"]');
  await page.waitForTimeout(150);
}
const live = page => page.locator('#addDevLive').innerText();
const options = page => page.$$eval('#adResults [role="option"]', els => els.map(e => e.dataset.adTpl));
const railLabels = page => page.$$eval('#addDevRail li .dev-shell-steplbl', els => els.map(e => e.textContent.trim()));
const current = page => page.getAttribute('#addDevRail li[aria-current="step"]', 'data-shell-step');

try {
  // ── A ───────────────────────────────────────────────────────────────
  {
    const page = await open();
    await page.click('#devicesPage [data-action="openDeviceWizard"]');
    await page.waitForSelector('#addDeviceModal.active');
    check('A the Add button opens the new flow (no flag)', await isOpen(page, 'addDeviceModal')
          && (await page.locator('#deviceWizardModal').count()) === 0);
    check('A its footer line is a live region (L3-F8)', (await page.getAttribute('#addDevLive', 'role')) === 'status'
          && (await page.getAttribute('#addDevLive', 'aria-live')) === 'polite');
    check('A no page error', page.errs.length === 0, page.errs);
  }

  // ── B … G ───────────────────────────────────────────────────────────
  {
    const page = await open();
    await openAdd(page);
    check('B the new dialog opens', await isOpen(page, 'addDeviceModal'));
    check('B the search has the focus', await active(page) === 'adQ');
    check('B the rail is an ordered list, the first step current', (await page.locator('#addDevRail').evaluate(e => e.tagName)) === 'OL'
          && await current(page) === 'what' && (await railLabels(page)).length === 4);
    check('B Back hidden on the first step', await page.locator('#addDevBack').isHidden());
    check('B Next aria-disabled (focusable), its reason on the line',
          (await page.getAttribute('#addDevNext', 'aria-disabled')) === 'true' && !(await page.evaluate(() => document.getElementById('addDevNext').disabled))
          && /Pick a model/.test(await page.locator('#addDevSummary').innerText()));
    await page.click('#addDevNext', { force: true });
    await page.waitForTimeout(150);
    check('B Next pressed anyway: the reason is said, the step stays', /Pick a model/.test(await live(page)) && await current(page) === 'what');
    const chips = await page.$$eval('#adChips [role="radio"]', els => els.map(e => ({ id: e.dataset.adType, on: e.getAttribute('aria-checked'), n: +e.querySelector('.find-chip-n').textContent, text: e.textContent.trim() })));
    check('B the type chips: a radiogroup, Any first and chosen, each with its count',
          (await page.getAttribute('#adChips', 'role')) === 'radiogroup' && chips[0].id === '' && chips[0].on === 'true'
          && /Any type/.test(chips[0].text) && chips.length > 2 && chips.every(c => c.n > 0), chips);
    check('B the models: a listbox, as many as Any counts', (await page.getAttribute('#adResults', 'role')) === 'listbox'
          && (await options(page)).length === chips[0].n, [(await options(page)).length, chips[0].n]);
    const card = page.locator('#adResults [data-ad-tpl="eastron_sdm630"]');
    check('B a card shows its ports', /RS-485/.test(await card.innerText()) && /Ethernet/.test(await card.innerText()));
    check('B the measured way is set apart, the three ways always there', await page.locator('[data-ad-way]').count() === 3);

    // C
    await page.fill('#adQ', 'sdm630');
    await page.waitForTimeout(800);
    let ids = await options(page);
    check('C search a model', ids.includes('eastron_sdm630') && ids.every(i => /sdm630/.test(i)), ids);
    check('C the count is said once typing pauses', /\d+ models?/.test(await live(page)), await live(page));
    await page.fill('#adQ', 'eastron');
    ids = await options(page);
    check('C search a brand', ids.length >= 2 && ids.every(i => i.startsWith('eastron')), ids);
    await page.fill('#adQ', 'contor');
    await page.waitForTimeout(100);
    const subs = await page.$$eval('#adResults .ad-card-sub', els => els.map(e => e.textContent));
    check('C a Romanian synonym (contor) finds the meters', subs.length > 2 && subs.every(s => /Meter/.test(s)), subs.slice(0, 5));
    await page.fill('#adQ', 'zzzz-no-such');
    check('C nothing matches: said', /No model matches/.test(await page.locator('#adResults').innerText()));
    await page.fill('#adQ', '');

    // D
    await page.focus('#adChips [role="radio"][aria-checked="true"]');
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(100);
    const second = chips[1];
    check('D ArrowRight chooses the next type and keeps the focus on it',
          (await page.getAttribute(`#adChips [data-ad-type="${second.id}"]`, 'aria-checked')) === 'true'
          && (await page.evaluate(() => document.activeElement?.dataset?.adType)) === second.id);
    check('D the list follows the type, its count', (await options(page)).length === second.n);
    await page.keyboard.press('Home');
    check('D Home: Any again', (await page.getAttribute('#adChips [data-ad-type=""]', 'aria-checked')) === 'true'
          && (await options(page)).length === chips[0].n);

    // E
    await page.fill('#adQ', 'eastron');
    await page.focus('#adQ');
    await page.keyboard.press('ArrowDown');
    const first = await page.evaluate(() => document.activeElement?.dataset?.adTpl);
    check('E ArrowDown from the search: into the models', !!first && first.startsWith('eastron'), first);
    await page.keyboard.press('ArrowDown');
    const pick = await page.evaluate(() => document.activeElement?.dataset?.adTpl);
    check('E ArrowDown moves along the models', pick && pick !== first);
    await page.keyboard.press(' ');
    check('E Space picks: aria-selected, Next enabled, the line names it',
          (await page.getAttribute(`#adOpt-${pick}`, 'aria-selected')) === 'true'
          && (await page.getAttribute('#addDevNext', 'aria-disabled')) === null
          && /Model:/.test(await page.locator('#addDevSummary').innerText()));
    await page.keyboard.press('Enter');
    await page.waitForTimeout(150);
    check('E Enter goes on: the connection step, its heading focused', await current(page) === 'connection' && await active(page) === 'addDevH-connection');
    await page.waitForSelector('#csAddPortKindQ');
    check('E the Connection step itself (conn-step.js): its first question, the port',
          (await page.locator('[data-shell-panel="connection"] #csAddPortKindQ').count()) === 1);
    check('E the first step is now a done step, a button back', await page.locator('#addDevRail button[data-shell-goto="what"]').count() === 1);
    check('E Next is the page\'s to judge here (the form, then the gateway)', (await page.getAttribute('#addDevNext', 'aria-disabled')) === null);
    await page.click('#addDevBack');
    await page.waitForTimeout(150);
    check('E Back keeps the search and the pick', await current(page) === 'what' && (await page.inputValue('#adQ')) === 'eastron'
          && (await page.getAttribute(`#adOpt-${pick}`, 'aria-selected')) === 'true');
    await page.click('#addDevNext', { force: true });
    await page.click('#addDevRail button[data-shell-goto="what"]');
    await page.waitForTimeout(100);
    check('E the rail\'s done step goes back too', await current(page) === 'what');

    // F
    await page.click('[data-ad-way="connection"]');
    await page.waitForTimeout(150);
    check('F By connection: the first two steps swapped', (await railLabels(page)).join('|') === 'Connection|What is it?|Check|Name & save', await railLabels(page));
    check('F …the way said in the live region', /Steps: Connection/.test(await live(page)));
    await page.click('[data-shell-panel="connection"] [data-ad-way="model"]');
    await page.waitForTimeout(150);
    check('F Choose a model instead: back, everything kept', (await railLabels(page))[0] === 'What is it?'
          && (await page.inputValue('#adQ')) === 'eastron' && (await page.getAttribute(`#adOpt-${pick}`, 'aria-selected')) === 'true');
    await page.click('[data-ad-way="many"]');
    await page.waitForSelector('[data-shell-panel="many"] #adManyConn #adManyPortKind-serial', { state: 'attached' });
    check('F Many of the same: the connection step, the model chosen kept (L3.7)', /Several Eastron/.test(await page.locator('[data-shell-panel="many"]').innerText()));
    await page.click('[data-shell-panel="many"] [data-ad-way="model"]');

    // G
    const n0 = page.dlg.seen.length;
    page.dlg.answers.push(false);                        // Keep editing
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    check('G Esc with something chosen asks first (Discard?)', page.dlg.seen.length === n0 + 1 && /Discard/.test(page.dlg.seen[n0].title)
          && await isOpen(page, 'addDeviceModal'));
    check('G …Keep editing keeps it', (await page.inputValue('#adQ')) === 'eastron');
    page.dlg.answers.push(true);                         // Discard
    await page.click('#addDevCancel');
    await page.waitForTimeout(300);
    check('G Cancel asks too; Discard closes', !(await isOpen(page, 'addDeviceModal')));
    // J: opened again it starts afresh; a model picked → its Connection step
    await openAdd(page);
    check('G opened again: a fresh start', (await page.inputValue('#adQ')) === '' && await current(page) === 'what');
    await page.click('#adResults [data-ad-tpl="eastron_sdm120"]');
    await page.click('#addDevNext', { force: true });
    await page.waitForSelector('#csAddPortKindQ');
    check('J a model picked: its Connection step, in the same dialog', await isOpen(page, 'addDeviceModal') && await current(page) === 'connection');
    check('J …and no question asked on the way', page.dlg.seen.length === n0 + 2);
    page.dlg.answers.push(true);
    await page.click('#addDevCancel');
    await page.waitForTimeout(300);
    check('B–J no page error', page.errs.length === 0, page.errs);
  }

  // ── H: the measured path, end to end (flag in this browser) ─────────
  {
    const page = await open();
    await openAdd(page);
    await page.click('[data-ad-way="measured"]');
    await page.waitForSelector('#adKinds');
    check('H measured: three steps, the first asks what it is', (await railLabels(page)).join('|') === 'What is it?|Meters|Name & save'
          && await active(page) === 'addDevH-what');
    await page.click('#addDevNext', { force: true });
    await page.waitForTimeout(150);
    check('H no type yet: Next said why, stays', /Say what it is/.test(await live(page)) && await current(page) === 'what');
    await page.click('label:has(input[name="adKind"][value="ups"])');
    check('H a type picked: Next goes on', (await page.getAttribute('#addDevNext', 'aria-disabled')) === null);
    await page.click('#addDevNext', { force: true });
    await page.waitForSelector('#adMtrRows[data-ready="1"]');
    check('H Meters: the Meters tab\'s rows, the heading focused', await current(page) === 'meters' && await active(page) === 'addDevH-meters');
    await page.selectOption('#adMtrRole-0', 'input');
    await page.selectOption('#adMtrDev-0', 'em1');
    await page.waitForSelector('#adMtrPower-0 option[value="power_active_total"]', { state: 'attached' });
    await page.selectOption('#adMtrPower-0', 'power_active_total');
    await page.click('#adMtrAdd');
    await page.waitForSelector('#adMtrRole-1');
    await page.selectOption('#adMtrRole-1', 'output');
    await page.selectOption('#adMtrDev-1', 'em2');
    await page.waitForSelector('#adMtrPower-1 option[value="power_active_total"]', { state: 'attached' });
    await page.selectOption('#adMtrPower-1', 'power_active_total');
    await page.click('#addDevNext', { force: true });
    await page.waitForSelector('#adName');
    await page.waitForTimeout(150);
    check('H Name & save: the type and both meters named', await current(page) === 'save'
          && /UPS/.test(await page.locator('.ad-facts').innerText()) && /Cabinet meter \(Input\)/.test(await page.locator('.ad-facts').innerText())
          && /Garage meter \(Output\)/.test(await page.locator('.ad-facts').innerText()), await page.locator('.ad-facts').innerText());
    check('H Next reads Create device', /Create device/.test(await page.locator('#addDevNext').innerText()));
    await page.fill('#adName', 'E2E UPS pair');
    check('H the id follows the name', (await page.inputValue('#adId')) === NEW_ID);
    // Back to Meters through the rail: the rows kept, then on again
    await page.click('#addDevRail button[data-shell-goto="meters"]');
    await page.waitForTimeout(150);
    check('H the rail back to Meters keeps both rows', (await page.inputValue('#adMtrDev-0')) === 'em1' && (await page.inputValue('#adMtrDev-1')) === 'em2');
    await page.click('#addDevNext', { force: true });
    await page.waitForSelector('#adName');
    check('H …and Name & save keeps the name typed', (await page.inputValue('#adName')) === 'E2E UPS pair' && (await page.inputValue('#adId')) === NEW_ID);
    const posted = page.waitForRequest(r => r.method() === 'POST' && new URL(r.url()).pathname === '/api/devices' && !/dry_run/.test(r.url()));
    const answered = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/devices' && !/dry_run/.test(r.url()));
    await page.click('#addDevNext', { force: true });
    const body = (await posted).postData();
    const resp = await answered;
    // what the old wizard posts for the same answers (app-meters.js _devWizCreateMeterOnly);
    // a meter picked gets its usual energy counter too (the rows' own choice)
    const meters = [{ role: 'input', device: 'em1', power: 'power_active_total', energy: 'energy_active_import', sign: 'auto' },
                    { role: 'output', device: 'em2', power: 'power_active_total', energy: 'energy_active_import', sign: 'auto' }];
    const old = JSON.stringify({ id: NEW_ID, name: 'E2E UPS pair', kind: 'ups', enabled: true, connection: { protocol: 'none' }, meters,
                                 mqtt: {}, influxdb: { bucket: undefined, device_tag: undefined }, ha_discovery_enabled: true });
    check('H the create is byte-identical to the old wizard\'s', body === old, [body, old]);
    check('H created', resp.ok(), resp.status());
    await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'));
    const dev = (await (await api.get(`/api/devices/${NEW_ID}`)).json()).device || {};
    check('H the UPS exists, measured by em1 (input) and em2 (output)', (dev.type || dev.kind) === 'ups'
          && JSON.stringify((dev.meters || []).map(m => [m.role, m.device])) === JSON.stringify([['input', 'em1'], ['output', 'em2']]), dev.meters);
    check('H closed without asking (nothing left to lose)', page.dlg.seen.length === 0, page.dlg.seen);
    check('H no page error', page.errs.length === 0, page.errs);
  }

  // ── I: Found (n) ────────────────────────────────────────────────────
  {
    const page = await open({ route: p => p.route('**/api/mqtt-discovery', r => r.fulfill({ json: { enabled: true, connected: true, items: [
      { key: 'shelly-1', state: 'new', name: 'Shelly 3EM hall', kind: 'meter', template: 'shelly_3em', device_id: 'shelly-1', connection: { protocol: 'mqtt', topic: 'shellies/shelly-1' } },
      { key: 'ruuvi-1', state: 'in_mbg', name: 'Ruuvi', kind: 'sensor', template: 'ruuvi', connection: { protocol: 'mqtt', topic: 'ruuvi/1' } }], rules: [] } })) });
    await openAdd(page);
    await page.waitForSelector('#adFoundChip:not([hidden])');
    check('I the Found chip counts the new ones only', /Found \(1\)/.test(await page.locator('#adFoundChip').innerText()));
    await page.click('#adFoundChip');
    check('I clicked: the list, aria-expanded', (await page.getAttribute('#adFoundChip', 'aria-expanded')) === 'true'
          && /Shelly 3EM hall/.test(await page.locator('#adFoundPanel').innerText()));
    check('I Use: aria-disabled (its model is not here), its reason beside it', (await page.getAttribute('[data-ad-use="0"]', 'aria-disabled')) === 'true'
          && /not on this gateway/.test(await page.locator('#adFoundW-0').innerText()));
    check('I no page error', page.errs.length === 0, page.errs);
  }

  // ── M: the hand-over keeps the protocol ─────────────────────────────
  {
    const page = await open();
    for (const [id, mode] of [['victron_gx_mqtt', 'mqtt'], ['pylontech_can_inverter', 'can']]) {
      await openAdd(page);
      await page.fill('#adQ', '');
      await page.click(`#adResults [data-ad-tpl="${id}"]`);
      await page.click('#addDevNext', { force: true });
      await page.waitForSelector('#csAddPortKindQ');
      await page.waitForTimeout(400);
      const w = await page.evaluate(() => ({ tpl: window.app._ad?.s?.template, port: document.querySelector('[name="csAddPortKind"]:checked')?.value,
                                             fact: !!document.querySelector('#csAddPortKindQ .cs-fact') }));
      const port = { mqtt: 'mqtt', can: 'can' }[mode];
      check(`M ${mode}: the Connection step on the model's port, said as a fact`, w.tpl === id && w.port === port && w.fact, w);
      page.dlg.answers.push(true);
      await page.click('#addDevCancel');
      await page.waitForFunction(() => !document.querySelector('#addDeviceModal.active'));
    }
    check('M no page error', page.errs.length === 0, page.errs);
  }

  // ── N: what the dirty guard counts ──────────────────────────────────
  {
    const page = await open();
    await openAdd(page);
    await page.fill('#adQ', 'sdm');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    check('N a search alone: Esc closes without asking', !(await isOpen(page, 'addDeviceModal')) && page.dlg.seen.length === 0);
    await openAdd(page);
    await page.click('[data-ad-way="measured"]');
    await page.click('#adTypeAddBtn');
    await page.fill('#adTnEn', 'Chiller unit');
    page.dlg.answers.push(false);                        // Keep editing
    await page.click('#addDevCancel');
    await page.waitForTimeout(300);
    check('N words typed in + Add type… are asked about', page.dlg.seen.length === 1 && await isOpen(page, 'addDeviceModal')
          && (await page.inputValue('#adTnEn')) === 'Chiller unit');
    check('N no page error', page.errs.length === 0, page.errs);
  }

  // ── O: open at once, busy; a failed load said ───────────────────────
  {
    const page = await open({ route: p => p.route('**/api/device-templates', async r => {
      await new Promise(res => setTimeout(res, 1500)); await r.continue(); }) });
    await page.click('#devicesPage [data-action="openDeviceWizard"]');
    await page.waitForTimeout(500);
    check('O the dialog is open before the catalog arrives, busy', await isOpen(page, 'addDeviceModal')
          && (await page.getAttribute('#addDevMain', 'aria-busy')) === 'true');
    await page.waitForSelector('#addDeviceModal.active #adResults [role="option"]');
    check('O loaded: not busy, the search focused', (await page.getAttribute('#addDevMain', 'aria-busy')) === null && await active(page) === 'adQ');
    const bad = await open({ route: p => p.route('**/api/device-templates', r => r.fulfill({ status: 500, body: 'boom' })) });
    await bad.click('#devicesPage [data-action="openDeviceWizard"]');
    await bad.waitForSelector('#addDeviceModal.active #addDevErrors .dev-shell-errbox');
    check('O a failed load is said in words', /could not be read/.test(await bad.locator('#addDevErrors').innerText())
          && (await bad.getAttribute('#addDevMain', 'aria-busy')) === null);
    const fp = await open({ route: p => p.route('**/api/mqtt-discovery', r => r.fulfill({ json: { items: [
      { key: 'k1', state: 'new', name: 'Node', kind: 'sensor', template: 't', connection: { topic: 'a/b' } }] } })) });
    await openAdd(fp);
    await fp.waitForSelector('#adFoundChip:not([hidden])');
    await fp.click('#adFoundChip');
    const st = await fp.$eval('[data-ad-use="0"]', e => ({ op: getComputedStyle(e).opacity, cur: getComputedStyle(e).cursor }));
    check('O Found\'s disabled Use looks disabled', +st.op < 1 && st.cur === 'not-allowed', st);
  }

  // ── K: Romanian ─────────────────────────────────────────────────────
  {
    const page = await open({ lang: 'ro' });
    await openAdd(page);
    check('K ro: the step, the chip, the ways', (await railLabels(page))[0] === 'Ce este?'
          && /Orice tip/.test(await page.locator('#adChips').innerText())
          && /Nu comunică — îl măsoară alte contoare/.test(await page.locator('.ad-ways').innerText()));
    check('K ro: Next\'s reason', /Alege un model/.test(await page.locator('#addDevSummary').innerText()));
    await page.fill('#adQ', 'eastron');
    await page.waitForTimeout(800);
    check('K ro: the count in Romanian plural', /\d+ (de )?modele|1 model/.test(await live(page)), await live(page));
    check('K no page error', page.errs.length === 0, page.errs);
  }

  // ── L: a phone ──────────────────────────────────────────────────────
  {
    const page = await open({ viewport: { width: 390, height: 844 } });
    await openAdd(page);
    const box = await page.locator('#addDeviceContent').boundingBox();
    check('L full screen', Math.round(box.width) === 390 && Math.round(box.height) === 844, box);
    check('L the rail is one line', await page.locator('#addDevRail').isHidden()
          && (await page.locator('#addDevStepLine').innerText()) === 'Step 1 of 4 · What is it?');
    const wide = await page.evaluate(() => [...document.querySelectorAll('#addDeviceContent *')]
      .filter(e => e.getClientRects().length && e.getBoundingClientRect().right > window.innerWidth + 1).map(e => e.id || e.className).slice(0, 5));
    check('L nothing wider than the screen', wide.length === 0, wide);
    check('L the footer is on screen', (await page.locator('#addDevNext').boundingBox()).y + 10 < 844);
    await page.click('[data-ad-way="measured"]');
    await page.waitForTimeout(150);
    check('L the line follows the way', (await page.locator('#addDevStepLine').innerText()) === 'Step 1 of 3 · What is it?');
    check('L no page error', page.errs.length === 0, page.errs);
  }
} catch (e) {
  check('no exception', false, String(e && e.stack || e));
} finally {
  await api.delete(`/api/devices/${NEW_ID}`);
  for (const c of contexts) await c.close().catch(() => {});
  await browser.close();
  await api.dispose();
}

const bad = results.filter(r => !r.ok);
console.log(`\n${results.length - bad.length}/${results.length} passed`);
process.exit(bad.length ? 1 : 0);
