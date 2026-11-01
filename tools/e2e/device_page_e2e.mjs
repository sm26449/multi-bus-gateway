// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The device page as a user meets it (lot 8a, L3.3): its tabs in four
// groups — Live, Data, Setup, Diagnose — walked by keyboard (one tab stop
// per bar, arrows between, Enter opens), reached by link (a tab that left —
// `edit`, `connection`, `meters`… — opens Setup at its section, a group's
// name opens its tab), a tab that cannot show anything says why; the header
// says the device's kind and health and keeps its actions in one ⋯ menu
// that gives the focus back; the meters editor (⋯ → Meters…) names the
// meters that measure a device, saves only the thresholds that were changed
// (the defaults are never written, a reset sends {}), says in words why a
// value is not published, and a meter's own editor lists what it measures;
// a device measured only by its meters is added from Add Device (no
// connection, then "Meters & kind"), renamed from the Edit modal's Identity
// without an address, and keeps its last meter; a viewer reads the Meters
// in Setup; on a phone the header takes two lines and the tab strip one row
// that scrolls.
//
// Against the throwaway gateway of the installation_safety fixture, writes
// on (every address the container's own loopback):
//   tools/e2e/fixture_up.sh safety 18087 --writes
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 OUT=<dir> node tools/e2e/device_page_e2e.mjs
// It adds dp-* devices and removes them at the end.
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab, openDeviceGroup, openDeviceEditor, saveDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const J = { 'Content-Type': 'application/json' };
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: J, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const meter = (id, port) => ({ id, name: `Meter ${id}`, template: 'eastron_sdm630', enabled: false,
  connection: { protocol: 'tcp', host: '127.0.0.1', port, unit_id: 1 } });
const PAIR = [{ role: 'input', device: 'dp-in', power: 'power_active_total' },
              { role: 'output', device: 'dp-out', power: 'power_active_total' }];
const same = (a, b) => JSON.stringify(Object.entries(a || {}).sort()) === JSON.stringify(Object.entries(b || {}).sort());
const row = async id => (await api('/api/devices')).body.devices.find(d => d.id === id);

async function cleanup() {
  for (const id of ['dp-ups', 'dp-chg', 'dp-wiz', 'dp-in', 'dp-out', 'dp-plain']) await api(`/api/devices/${id}`, { method: 'DELETE' });
}
async function setup() {
  await cleanup();
  for (const d of [meter('dp-in', 15700), meter('dp-out', 15701), meter('dp-plain', 15702)]) {
    const r = await api('/api/devices', { method: 'POST', body: JSON.stringify(d) });
    if (r.status !== 200) throw new Error(`create ${d.id}: ${JSON.stringify(r.body)}`);
  }
  const r = await api('/api/devices', { method: 'POST', body: JSON.stringify({ id: 'dp-ups', name: 'UPS rack 1', kind: 'ups',
    connection: { protocol: 'none' }, meters: PAIR, options: { tolerance_pct: 3 } }) });
  if (r.status !== 200) throw new Error(`create dp-ups: ${JSON.stringify(r.body)}`);
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(role, opts = {}) {
  const page = await browser.newPage({ viewport: opts.viewport || { width: 1440, height: 950 }, colorScheme: opts.theme || 'light' });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.dlg.manual = true;
  if (role) await page.route('**/api/auth/status', r => r.fulfill({ json: { enabled: true, role, modules: [] } }));
  await page.goto(BASE + (opts.hash || '/'));
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const go = async (page, hash) => { await page.evaluate(h => { location.hash = h; }, hash); await page.waitForTimeout(1200); };
const active = page => page.evaluate(() => document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab);
const activeGroup = page => page.evaluate(() => document.querySelector('#deviceWsGroups .active')?.dataset.dgroup);
const shown = page => page.locator('#deviceWsTabs [data-dtab]:visible').evaluateAll(els => els.map(e => e.dataset.dtab));
const focused = page => page.evaluate(() => document.activeElement?.id || document.activeElement?.dataset?.dtab || document.activeElement?.tagName);

try {
  await setup();
  const page = await open(null);
  await go(page, '#/devices/sunfield-u1');
  await page.waitForSelector('#deviceWsGroups');

  // ── groups and tabs ──
  const groups = await page.locator('#deviceWsGroups [role=tab]').evaluateAll(els => els.map(e => e.dataset.dgroup));
  check('four groups, as tabs: Live, Data, Setup, Diagnose', groups.join() === 'live,data,setup,diagnose', groups.join());
  const panelOk = await page.evaluate(() => {
    const p = document.getElementById('dgrpPanel');
    const g = document.querySelector('#deviceWsGroups .active');
    return p?.getAttribute('role') === 'tabpanel' && p.getAttribute('aria-labelledby') === g?.id
      && g?.getAttribute('aria-controls') === 'dgrpPanel' && p.contains(document.getElementById('deviceWsTabs'));
  });
  check('the group tab controls one tabpanel that holds the tab strip (valid ARIA)', panelOk);
  check('it opens on Live → Overview', (await activeGroup(page)) === 'live' && (await active(page)) === 'overview');
  const live = await shown(page);
  check('Live: Overview and History, no Monitor', live[0] === 'overview' && live.includes('history') && !live.includes('monitor'), live.join());
  const tabPanels = await page.evaluate(() => [...document.querySelectorAll('#deviceWsTabs [role=tab]')].every(t =>
    document.getElementById(t.getAttribute('aria-controls'))?.getAttribute('aria-labelledby') === t.id));
  check('every tab names its panel and the panel its tab', tabPanels);

  // ── keyboard: one tab stop per bar, arrows move, Enter opens ──
  const stops = await page.evaluate(() => ['#deviceWsGroups', '#deviceWsTabs'].map(s =>
    [...document.querySelectorAll(`${s} [role=tab]`)].filter(t => !t.hidden && t.tabIndex === 0).length));
  check('one tab stop per bar (roving tabindex)', stops.join() === '1,1', stops.join());
  await page.focus('#dgrp-live');
  await page.keyboard.press('ArrowRight');
  check('→ moves to Data without opening it', (await focused(page)) === 'dgrp-data' && (await activeGroup(page)) === 'live');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(400);
  check('Enter opens Data on its first tab', (await activeGroup(page)) === 'data' && (await active(page)) === 'measurements', await active(page));
  await page.keyboard.press('End');
  check('End moves to the last group', (await focused(page)) === 'dgrp-diagnose');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(400);
  const diag = await shown(page);
  check('Diagnose holds Monitor and Logs, and opens on Logs', diag.join() === 'monitor,logs' && (await active(page)) === 'logs', diag.join());

  // ── by link ──
  await go(page, '#/devices/sunfield-u1/edit');
  check('#…/edit (a tab that left) opens Setup at its connection, the address says setup',
        (await active(page)) === 'setup' && /\/setup$/.test(await page.evaluate(() => location.hash))
        && await page.evaluate(() => document.activeElement?.closest('[data-setup]')?.dataset.setup) === 'connection',
        await page.evaluate(() => location.hash));
  await go(page, '#/devices/sunfield-u1/configure');
  check('#…/configure (the group that left) opens Setup', (await activeGroup(page)) === 'setup');
  await go(page, '#/devices/sunfield-u1/data');
  check('a group\'s name opens that group', (await activeGroup(page)) === 'data');
  await go(page, '#/devices/dp-plain/monitor');
  const off = await page.evaluate(() => {
    const n = document.querySelector('#deviceDetailView [data-dpanel="monitor"] .dpanel-off');
    return n && !n.hidden ? n.textContent.trim() : '';
  });
  check('Monitor of a device not polling opens, and says why', (await active(page)) === 'monitor' && /polling/i.test(off), off);
  check('…and the tab says it too, to a screen reader', await page.evaluate(() =>
    document.getElementById('dtab-monitor').getAttribute('aria-disabled') === 'true'
    && /polling/i.test(document.getElementById(document.getElementById('dtab-monitor').getAttribute('aria-describedby'))?.textContent || '')));
  await go(page, '#/devices/dp-plain');
  check('a plain open lands on Live → Overview again', (await active(page)) === 'overview');

  // ── one vocabulary for health: header and Overview ──
  await go(page, '#/devices/sunfield-u1');
  const words = await page.evaluate(() => [document.querySelector('#ddvMeta .ddv-verdict')?.textContent.trim(),
    [...document.querySelectorAll('[data-dpanel="overview"] span')].map(x => x.textContent.trim())]);
  check('a unit\'s Overview says its health in the header\'s words', words[1].includes(words[0]), JSON.stringify(words[0]));
  check('the Overview panel is a tab stop', (await page.getAttribute('#dpanel-overview', 'tabindex')) === '0');
  await go(page, '#/devices/dp-plain');
  const monName = await page.evaluate(() => {
    const b = document.getElementById('dtab-monitor');
    return { inside: !!b.querySelector('#dtab-monitor-why'), desc: b.getAttribute('aria-describedby') };
  });
  check('an off tab\'s reason is its description, not part of its name', monName.inside === false && monName.desc === 'dtab-monitor-why');

  // ── header ──
  check('the header says its kind', await page.locator('#deviceDetailView h2 .kind-ico').count() === 1);
  check('…and its health, in words', /\S/.test(await page.locator('#ddvMeta .ddv-verdict').innerText()));
  await page.click('#ddvActions');
  const items = await page.locator('#ddvActionsMenu [role=menuitem]:visible').allInnerTexts();
  // the item was "Kind…" until L2 named it "Type…" (1fa53ca)
  check('⋯ opens the actions: Type and Delete', items.some(x => /^Type/.test(x)) && items.some(x => /Delete/.test(x)), items.join(' | '));
  check('…the first item takes the focus', await page.evaluate(() => document.activeElement?.getAttribute('role') === 'menuitem'));
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Escape');
  check('Esc closes it and gives the focus back to ⋯', (await focused(page)) === 'ddvActions'
        && await page.locator('#ddvActionsMenu').isHidden());
  await page.click('#ddvActions');
  await page.focus('#ddvActions');
  await page.keyboard.press('Escape');
  check('Esc on ⋯ itself closes an open menu', await page.locator('#ddvActionsMenu').isHidden());

  // ── Disable / Enable from ⋯ (nothing uses dp-plain) ──
  await api('/api/devices/dp-plain', { method: 'PUT', body: JSON.stringify({ enabled: true }) });
  await page.evaluate(() => window.app.openDeviceDetail('dp-plain'));     // drawn again: now enabled
  await page.waitForTimeout(800);
  await page.click('#ddvActions');
  await page.click('#ddvEnableToggle');
  await page.waitForSelector('#appDialog.active');
  check('Disable asks first (a write confirm, not a red one)', !(await page.locator('#appDialogOk.btn-danger').count()));
  await page.click('#appDialogOk');
  await page.waitForFunction(() => !document.getElementById('appDialog').classList.contains('active'));
  await page.waitForTimeout(900);
  check('…then it is disabled', (await row('dp-plain')).enabled === false);
  await page.click('#ddvActions');
  check('…and ⋯ now offers Enable', /Enable/.test(await page.locator('#ddvEnableToggle').innerText()));
  await page.click('#ddvEnableToggle');
  await page.waitForTimeout(1200);
  check('Enable asks nothing and enables it', (await row('dp-plain')).enabled === true);
  await api('/api/devices/dp-plain', { method: 'PUT', body: JSON.stringify({ enabled: false }) });

  // ── Meters: the UPS ──
  await go(page, '#/devices/dp-ups');
  check('a meter-only device: no Measurements tab', await page.locator('#deviceWsTabs [data-dtab="measurements"]').count() === 0);
  const meta = await page.locator('#ddvMeta').innerText();
  check('…the header names its meters by name, and says they are not reporting',
        /Meter dp-in, Meter dp-out/.test(meta) && /meters are not reporting/i.test(meta), meta);
  check('…its Test is held, saying it has nothing to test (L3.10: the header keeps Test, with why)',
        (await page.getAttribute('#ddvHeadTest', 'aria-disabled')) === 'true' && /measured by its meters/i.test(await page.getAttribute('#ddvHeadTest', 'title') || ''));
  check('…its kind can only be one a meter-only device may be', await (async () => {
    const r1 = await api('/api/devices/dp-ups/organise', { method: 'PATCH', body: JSON.stringify({ kind: 'meter' }) });
    const r2 = await api('/api/devices/dp-ups/organise', { method: 'PATCH', body: JSON.stringify({ kind: null }) });
    return r1.status === 422 && r2.status === 422;
  })());
  await openDeviceEditor(page, 'meters');
  await page.waitForSelector('#mtrRows[data-ready]');
  check('⋯ → Meters… opens its meters editor', await page.locator('#ddvEditBar').isVisible()
        && /Meters/.test(await page.locator('#ddvEditTitle').innerText()));
  const roles = await page.$$eval('#mtrRows select[id^="mtrRole-"]', els => els.map(e => e.value));
  check('the two meters, in their roles', roles.join() === 'input,output', roles.join());
  check('a stored link shows its own fields (no counter guessed)',
        (await page.inputValue('#mtrEnergy-0')) === '' && (await page.inputValue('#mtrPower-0')) === 'power_active_total');
  check('the thresholds are offered for a pair', await page.locator('#mtrOptsBox').isVisible());
  const why = await page.locator('#mtrGives').innerText();
  check('what it gives: no value, and why — its meters are named as not reading', /Meter dp-in, Meter dp-out: not reading/i.test(why), why.slice(0, 300));
  check('…labels, not raw field names', !/power_loss|efficiency_pct/.test(why), why.slice(0, 300));
  check('Overview has the pair\'s one line', (await page.evaluate(() => !!document.getElementById('ddvPair'))) === true);

  const sent = [];
  await page.route('**/api/devices/dp-ups/meters', r => { if (r.request().method() === 'PUT') sent.push(r.request().postDataJSON()); r.continue(); });
  await page.evaluate(() => { document.getElementById('mtrOpts').open = true; });
  await page.fill('#mtrOpt-min_load_w', '250');
  await page.evaluate(() => window.app._loadDeviceUsedBy('dp-ups'));
  await page.waitForTimeout(800);
  check('an unsaved threshold survives a Used-by repaint', (await page.inputValue('#mtrOpt-min_load_w')) === '250');
  await page.click('#mtrSave');
  await page.waitForFunction(() => /✓/.test(document.getElementById('mtrFeedback')?.textContent || ''));
  const body = sent[sent.length - 1] || {};
  check('Save sends the device\'s own thresholds and the changed one — never a default, never null',
        same(body.options, { tolerance_pct: 3, min_load_w: 250 }), JSON.stringify(body.options));
  check('…stored as the device\'s own', same((await row('dp-ups')).meter_options_own, { tolerance_pct: 3, min_load_w: 250 }));
  await page.waitForSelector('#mtrSave');
  await page.click('#mtrSave');
  await page.waitForFunction(() => /✓/.test(document.getElementById('mtrFeedback')?.textContent || ''));
  check('Save with the thresholds untouched sends none (absent keeps them)', !('options' in (sent[sent.length - 1] || {})), JSON.stringify(sent[sent.length - 1]));
  await page.evaluate(() => { document.getElementById('mtrOpts').open = true; });
  await page.click('#mtrOptReset');
  await page.click('#mtrSave');
  await page.waitForFunction(() => /✓/.test(document.getElementById('mtrFeedback')?.textContent || ''));
  check('Reset to defaults sends {}', JSON.stringify((sent[sent.length - 1] || {}).options) === '{}', JSON.stringify(sent[sent.length - 1]));
  check('…and the device keeps none of its own', JSON.stringify((await row('dp-ups')).meter_options_own) === '{}');
  await page.unroute('**/api/devices/dp-ups/meters');

  // a wrong value is said at its field, nothing sent
  await page.evaluate(() => { document.getElementById('mtrOpts').open = true; });
  await page.fill('#mtrOpt-tolerance_pct', '99');
  await page.click('#mtrSave');
  await page.waitForTimeout(300);
  check('a threshold out of range is said at its field', (await page.getAttribute('#mtrOpt-tolerance_pct', 'aria-invalid')) === 'true');
  await page.fill('#mtrOpt-tolerance_pct', '');

  // ── a row with a role and no meter is said, never dropped ──
  await go(page, '#/devices/dp-plain');
  await openDeviceEditor(page, 'meters');
  await page.waitForSelector('#mtrRows[data-ready]');
  check('an empty row guesses no role', (await page.inputValue('#mtrRole-0')) === '');
  const plainPuts = [];
  await page.route('**/api/devices/dp-plain/meters', r => { plainPuts.push(r.request().method()); r.continue(); });
  await page.selectOption('#mtrRole-0', 'consumption');
  await page.click('#mtrSave');
  await page.waitForTimeout(400);
  check('a role with no meter: said at the meter, nothing sent', (await page.getAttribute('#mtrDev-0', 'aria-invalid')) === 'true'
        || (await page.evaluate(() => document.querySelector('#mtrDev-0')?._cbx?.input?.getAttribute('aria-invalid'))) === 'true');
  check('…nothing sent', !plainPuts.includes('PUT'), plainPuts.join());
  await page.unroute('**/api/devices/dp-plain/meters');
  const prim = (await api('/api/devices')).body.devices.find(d => d.primary);
  if (prim) {
    await go(page, `#/devices/${prim.id}`);
    await openDeviceEditor(page, 'meters');
    await page.waitForSelector('#mtrMeasures');
    check('the primary meter\'s Meters says what it measures (no rows of its own)',
          await page.locator('#mtrRows').count() === 0 && /Measures/.test(await page.locator('#mtrMeasures').innerText()));
  }
  await go(page, '#/devices/dp-ups');
  await openDeviceEditor(page, 'meters');
  await page.waitForSelector('#mtrRows[data-ready]');

  // ── Identity: renamed without an address (MD-N3) ──
  const puts = [];
  await page.route('**/api/devices/dp-ups', r => { if (['PUT', 'PATCH'].includes(r.request().method())) puts.push(r.request().postDataJSON()); r.continue(); });
  // its editor (Edit…, L3.4): the modal at its Identity — no Connection section
  await openDeviceTab(page, 'connection');
  check('its editor is its Identity: no Connection section, no address to fill',
        (await page.getAttribute('#devEditModal .dev-shell-tab[aria-selected="true"]', 'data-shell-sec')) === 'identity'
        && !(await page.locator('#devEditTab-connection').count())
        && await page.locator('#ddvHost, #ddvSerial, #ddvUrl, #ddvBroker').count() === 0);
  await page.fill('#ddvName', 'UPS rack one');
  const savedUps = await saveDeviceEditor(page);
  check('renamed: saved, and no connection was sent (its identity alone)',
        savedUps?.ok() && (await row('dp-ups')).name === 'UPS rack one'
        && JSON.stringify((puts[0] || {}).sections) === '{"identity":{"name":"UPS rack one"}}', JSON.stringify(puts[0]));
  await page.unroute('**/api/devices/dp-ups');

  // ── a meter's own page: what it measures ──
  await go(page, '#/devices/dp-in');
  await openDeviceEditor(page, 'meters');
  await page.waitForSelector('#mtrMeasures');
  const meas = await page.locator('#mtrMeasures').innerText();
  check('a meter\'s meters editor lists what it measures, and in which role', /UPS rack one/.test(meas) && /Input/.test(meas), meas);
  await page.click('#mtrMeasures a');
  await page.waitForTimeout(1500);
  check('…a link to that device\'s Meters (its Setup, at Meters)', /^#\/devices\/dp-ups\//.test(await page.evaluate(() => location.hash))
        && (await active(page)) === 'setup'
        && await page.evaluate(() => document.activeElement?.closest('[data-setup]')?.dataset.setup) === 'meters', await page.evaluate(() => [location.hash, document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab, document.activeElement?.outerHTML.slice(0, 90)].join(' ')));

  // ── Add Device: no connection, measured by meters ──
  await page.evaluate(() => window.app.navigateTo('devices'));
  // (L3.9: Add Device's "It doesn't communicate" way — What · Meters · Name & save)
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
  await page.click('[data-ad-way="measured"]');
  await page.waitForSelector('#adKinds');
  check('the measured way: What · Meters · Name & save', /Meters/.test(await page.locator('#addDevRail').innerText()));
  const kinds = await page.$$eval('input[name="adKind"]', els => els.map(e => e.value));
  check('the kinds the gateway offers: UPS first, never a meter or "other"', kinds[0] === 'ups' && !kinds.includes('meter') && !kinds.includes('other'), kinds.join());
  await page.click('#addDevNext', { force: true });   // aria-disabled still answers: it says why
  await page.waitForTimeout(400);
  check('Next without a kind: it says so, nothing moves', /Say what it is/.test(await page.locator('#addDevSummary').innerText())
        && await page.evaluate(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep) === 'what');
  await page.click('label:has(input[name="adKind"][value="charger"])');
  await page.click('#addDevNext');
  await page.waitForSelector('#adMtrRole-0');
  await page.selectOption('#adMtrRole-0', 'consumption');
  await page.selectOption('#adMtrDev-0', 'dp-plain');
  await page.waitForTimeout(500);
  await page.click('#addDevNext');
  await page.waitForSelector('#adName');
  await page.fill('#adName', 'Charger dp');
  await page.fill('#adId', 'dp-wiz');
  await page.click('#addDevNext');
  await page.waitForFunction(() => /\/devices\/dp-wiz\//.test(location.hash), null, { timeout: 8000 }).catch(() => {});
  await page.waitForSelector('#ddvEditBar:not([hidden])').catch(() => {});
  const made = await row('dp-wiz');
  check('created measured by its meter, and opened on its meters editor',
        made && made.protocol === 'none' && made.kind === 'charger' && (made.meters || [])[0]?.device === 'dp-plain'
        && /Meters/.test(await page.locator('#ddvEditTitle').innerText()), JSON.stringify(made && { p: made.protocol, k: made.kind, m: made.meters }));
  await page.waitForSelector('#mtrRows[data-ready] .mtr-rm button');
  check('…its only meter cannot be removed (it IS its meter)',
        (await page.getAttribute('#mtrRows .mtr-rm button', 'aria-disabled')) === 'true');
  if (OUT) await page.screenshot({ path: `${OUT}/devpage-meters-desktop-light.png`, fullPage: true });
  check('no page errors (admin)', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── a viewer ──
  const v = await open('viewer', { hash: '#/devices/dp-ups/connection' });
  await v.waitForSelector('#deviceWsGroups');
  await v.waitForTimeout(800);
  check('viewer: a link to Connection lands on Setup, read-only (L3-F16)', (await active(v)) === 'setup');
  await go(v, '#/devices/dp-ups/meters');
  await v.waitForSelector('[data-dpanel="setup"] [data-setup="meters"]');
  const vm = await v.locator('[data-dpanel="setup"] [data-setup="meters"]').innerText();
  check('viewer: the Meters read in Setup — both meters, no field, no Save',
        /dp-in/.test(vm) && /dp-out/.test(vm) && !(await v.locator('#mtrSave').count())
        && !(await v.locator('[data-dpanel="setup"] input:visible, [data-dpanel="setup"] select:visible').count()), vm);
  check('viewer: no Delete in ⋯', !(await v.locator('#ddvDeleteBtn:visible').count()));
  check('no page errors (viewer)', v.errs.length === 0, v.errs.join(' | '));
  await v.close();

  // ── a phone ──
  for (const theme of ['light', 'dark']) {
    const ph = await open(null, { viewport: { width: 390, height: 844 }, theme, hash: '#/devices/dp-ups' });
    await ph.waitForSelector('#deviceWsGroups');
    await ph.waitForTimeout(800);
    const wide = await ph.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(`phone (${theme}): the page never scrolls sideways`, wide <= 1, String(wide));
    await openDeviceGroup(ph, 'live');
    const strip = await ph.evaluate(() => {
      const b = [...document.querySelectorAll('#deviceWsTabs [role=tab]')].filter(t => !t.hidden);
      return new Set(b.map(t => Math.round(t.getBoundingClientRect().top))).size;
    });
    check(`phone (${theme}): the tabs are one row`, strip === 1, String(strip));
    if (OUT) {
      await openDeviceEditor(ph, 'meters');
      await ph.waitForSelector('#mtrRows[data-ready]');
      await ph.screenshot({ path: `${OUT}/devpage-meters-phone-${theme}.png`, fullPage: true });
    }
    await ph.close();
  }
  if (OUT) {
    for (const theme of ['light', 'dark']) {
      const d = await open(null, { theme, hash: '#/devices/sunfield-u1' });
      await d.waitForSelector('#deviceWsGroups');
      await d.waitForTimeout(1200);
      await d.screenshot({ path: `${OUT}/devpage-device-desktop-${theme}.png` });
      await d.close();
      const w = await open(null, { theme });
      await w.evaluate(() => window.app.openDeviceWizard());
      await w.waitForSelector('#addDeviceModal.active');
      await w.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
      await w.click('[data-ad-way="measured"]');
      await w.waitForSelector('#adKinds');
      await w.waitForTimeout(600);
      await w.screenshot({ path: `${OUT}/devpage-wizard-meteronly-${theme}.png` });
      await w.close();
    }
  }
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await cleanup().catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
