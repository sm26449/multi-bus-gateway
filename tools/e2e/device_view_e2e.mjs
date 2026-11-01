// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The device page as an operations view (ADR-0031 §3, L3.3), as each role
// meets it:
// - the header says the device in one technical line — the same line its
//   row on the Devices list and its bridge card say — with the write lock,
//   the outputs and "via" links (bridge, parent, installation);
// - the ⋯ menu is there for every role, its items gated by role (L3-F13),
//   in ADR-0032's fixed groups, the same on every tab, an item that cannot
//   run saying why under its name; walked by keyboard (menu button pattern);
//   a list row, a table row, a bridge line and an installation's unit row
//   open the same menu (L3.10);
// - the header operates: Test, Preview, Write… (disabled with its reason);
// - nothing on the page body edits: editing opens from ⋯ or the E key (the
//   Edit device modal, L3.4, or an editor of the page);
// - the tabs are Live · Data · Setup · Diagnose; Setup shows the
//   configuration read-only, to a viewer too (L3-F16); a link to a tab that
//   left (connection, outputs, attributes, meters, edit) opens Setup there;
// - a modal with unsaved changes asks before Esc or ✕ discards them (L3-F15);
// - on a phone (390 px) nothing scrolls sideways; Romanian has its words.
//
// Against a throwaway gateway of the qa fixture, login on:
//   MBG_FX_NAME=mbg-fx-view tools/e2e/fixture_up.sh qa 18331 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18331 MBG_PASS=e2e-pass node tools/e2e/device_view_e2e.mjs
//   docker rm -f mbg-fx-view
// It adds dv-gx (a GX with one battery component) and removes it at the end.
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { editorSection, saveDeviceEditor, closeDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18331';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : ''}`);
};

const api = await pwRequest.newContext({ baseURL: BASE });
const login = await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const get = async path => (await api.get(path)).json();

async function cleanup() {
  for (const id of ['dv-gx', 'dv-pq', 'dv-web']) await api.delete(`/api/devices/${id}`);
  await api.post('/api/devices/em1/rest-push', { data: { enabled: false, url: '' } }).catch(() => {});
  await api.put('/api/devices/ups1/meters', { data: { meters: [{ role: 'input', device: 'em1', power: 'power_active_total' }] } }).catch(() => {});
}
async function setup() {
  await cleanup();
  const r = await api.post('/api/devices', { data: { id: 'dv-gx', name: 'DV GX', template: 'victron_gx_mqtt',
    connection: { protocol: 'mqtt', broker: '127.0.0.1', port: 1883, topic: 'N/x/#', vars: { portal: 'x' } } } });
  if (!r.ok()) throw new Error(`create dv-gx: ${await r.text()}`);
  const c = await api.post('/api/devices/dv-gx/components', { data: { type: 'battery', instance: '288' } });
  if (!c.ok()) throw new Error(`component: ${await c.text()}`);
  // a meter that records PQ, an HTTP device whose URL carries secrets, a
  // REST push with a key in its query, a UPS between two meters
  const pq = await api.post('/api/devices', { data: { id: 'dv-pq', name: 'DV PQ', template: 'janitza_umg512_pro', enabled: false,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15503, unit_id: 1 } } });
  if (!pq.ok()) throw new Error(`create dv-pq: ${await pq.text()}`);
  const web = await api.post('/api/devices', { data: { id: 'dv-web', name: 'DV web', template: 'fronius_solar_api_inverter', enabled: false,
    connection: { protocol: 'http', url: 'http://user:s3cret@192.0.2.40:15993/solar_api/v1/x.cgi?token=abc123' } } });
  if (!web.ok()) throw new Error(`create dv-web: ${await web.text()}`);
  const rp = await api.post('/api/devices/em1/rest-push', { data: { enabled: true, url: 'http://127.0.0.1:9/ingest?apikey=zz9', interval_s: 30, format: 'native', verify_tls: true, headers: {} } });
  if (!rp.ok()) throw new Error(`rest push: ${await rp.text()}`);
  const mt = await api.put('/api/devices/ups1/meters', { data: { meters: [
    { role: 'input', device: 'em1', power: 'power_active_total' }, { role: 'output', device: 'em2', power: 'power_active_total' }] } });
  if (!mt.ok()) throw new Error(`ups1 meters: ${await mt.text()}`);
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const pages = [];
// a page as `role` sees it: the session is the admin's; the role the page
// believes is the one /api/auth/status says (what the UI gates on)
async function open(role, opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  pages.push(ctx);
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  if (opts.lang) await page.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, opts.lang);
  if (role && role !== 'admin') {
    await page.route('**/api/auth/status', r => r.fulfill({ json: { enabled: true, role, modules: [] } }));
  }
  await page.goto(BASE + (opts.hash || '/'));
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const go = async (page, hash) => {
  await page.evaluate(h => { location.hash = h; }, hash);
  await page.waitForSelector('#deviceDetailView #ddvFacts', { state: 'visible' }).catch(() => {});
  await page.waitForTimeout(700);
};
const facts = page => page.locator('#ddvFacts .dev-sum-text').innerText();
const menuIds = async (page, menu = '#ddvActionsMenu') =>
  page.locator(`${menu} [role=menuitem]`).evaluateAll(els => els.filter(e => e.offsetParent).map(e => e.dataset.ddvAct));
async function openMenu(page) {
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
}
async function closeMenu(page) {
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) === 'true') await page.keyboard.press('Escape');
}
const activeTab = page => page.evaluate(() => document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab);
const activeGroup = page => page.evaluate(() => document.querySelector('#deviceWsGroups .active')?.dataset.dgroup);
const visibleSaves = page => page.locator('#deviceDetailView button:visible').evaluateAll(els =>
  els.filter(b => /^\s*(save|save & apply|salvează)/i.test(b.textContent || '')).map(b => b.textContent.trim()));

try {
  check('logged in (the fixture\'s login)', login.ok());
  await setup();
  const list = (await get('/api/devices')).devices;
  const byId = Object.fromEntries(list.map(d => [d.id, d]));

  // ── A. the header line, per kind ─────────────────────────────────────
  const page = await open('admin');
  for (const [id, want] of [['rtu1', /^Modbus RTU · \/dev\/ttyQA1 · 9600 8N1 · unit 3 · Master/],
                            ['tap1', /Listen-only$/],
                            ['brpoll', /^Modbus RTU · bridge panel port 14197 · unit 4 · Master/],
                            ['em1', /^Modbus TCP · 127\.0\.0\.1:15502 · unit 1 · Master/],
                            ['ups1', /^Measured by its meters$/],
                            ['sunfield-u1', /^read via /],
                            ['dv-gx-battery-288', /^through DV GX · /]]) {
    await go(page, `#/devices/${id}`);
    const text = await facts(page);
    check(`A header line: ${id}`, want.test(text), text);
    if (byId[id]?.summary?.text && !['sunfield-u1', 'dv-gx-battery-288'].includes(id)) {
      check(`A …the gateway's own words (${id})`, text === byId[id].summary.text, `${text} ≠ ${byId[id].summary.text}`);
    }
  }
  await go(page, '#/devices/brpoll');
  check('A via: the bridge, as a link', await page.locator('#ddvFacts [data-via="bridge"]').count() === 1
        && /panel/.test(await page.locator('#ddvFacts [data-via="bridge"]').innerText()));
  await go(page, '#/devices/sunfield-u1');
  check('A via: the installation, as a link', (await page.getAttribute('#ddvFacts [data-via="installation"]', 'href')) === '#/installations/sunfield');
  await go(page, '#/devices/dv-gx-battery-288');
  check('A via: the parent device, as a link', (await page.getAttribute('#ddvFacts [data-via="parent"]', 'href')) === '#/devices/dv-gx');
  await go(page, '#/devices/rtu1');
  const lockTxt = await page.locator('#ddvFacts .dev-sum-lock').innerText();
  check('A the write lock, in words', /writes (locked|allowed)/i.test(lockTxt), lockTxt);
  const outTxt = await page.locator('#ddvFacts .dev-sum-out').allInnerTexts();
  check('A the outputs, in words: on, off, or why not', outTxt.some(x => /MQTT( on| off|: on)/.test(x)) && outTxt.some(x => /OPC UA( on| off|: on)/.test(x)), outTxt.join());
  // the fixture has no broker and no database: nothing publishes, nothing is green (L3-UX-7)
  check('A no output is green while it does not publish', await page.locator('#ddvFacts .dev-sum-out.on').count() === 0,
        await page.locator('#ddvFacts .dev-sum-out.on').allInnerTexts());
  await go(page, '#/devices/tap1');
  check('A a listen-only device never says "writes allowed"', !/writes allowed/i.test(await page.locator('#ddvFacts').innerText()));
  await go(page, '#/devices/rtu1');
  check('A the state line stays', /\S/.test(await page.locator('#ddvMeta').innerText()));

  // the same line on the list row and the bridge card (one render function)
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  await page.waitForSelector('#devicesList .device-row[data-f="rtu1"] .dev-sum-text');
  check('A the list row says the same line', (await page.locator('#devicesList .device-row[data-f="rtu1"] .dev-sum-text').innerText())
        === byId.rtu1.summary.text);
  check('A the bridge card says the same line', (await page.locator('.bridge-card .bridge-dev[data-f="brpoll"] .dev-sum-text').innerText())
        === byId.brpoll.summary.text);

  // ── B. the ⋯ menu per role (ADR-0032 §1–§3) ─────────────────────────
  const WANT_ADMIN = ['test', 'preview', 'reconnect', 'write', 'edit', 'connection', 'polling', 'outputs', 'bus-line',
                      'measurements', 'download-map', 'calculated', 'meters', 'template-update', 'type', 'about', 'duplicate',
                      'setup', 'lock', 'disable', 'delete', 'restore'];
  const item = (pg, id, menu = '#ddvActionsMenu') => pg.locator(`${menu} [data-ddv-act="${id}"]`);
  const why = async (pg, id, menu) => (await item(pg, id, menu).locator('.mbg-menu-why').count()) ? item(pg, id, menu).locator('.mbg-menu-why').innerText() : '';
  await go(page, '#/devices/rtu1');
  await openMenu(page);
  const adm = await menuIds(page);
  check('B admin: the full menu, in its groups', JSON.stringify(adm) === JSON.stringify(WANT_ADMIN), adm.join());
  check('B admin: six groups and the restore line apart by separators', await page.locator('#ddvActionsMenu [role=separator]').count() === 6);
  check('B admin: E names the item it opens (Edit connection… on Overview)',
        (await page.getAttribute('#ddvActionsMenu [data-ddv-act="connection"]', 'aria-keyshortcuts')) === 'E'
        && await page.locator('#ddvActionsMenu [aria-keyshortcuts]').count() === 1);
  // the fixture's gateway has writes off: Write… stays, disabled, saying so (L3-UX-4)
  check('B admin: Write… disabled, the reason under its name', (await item(page, 'write').getAttribute('aria-disabled')) === 'true'
        && /Writes are off for the gateway/.test(await why(page, 'write')), await why(page, 'write'));
  check('B admin: Reconnect stays, disabled while it runs', (await item(page, 'reconnect').getAttribute('aria-disabled')) === 'true');
  check('B admin: no "Disable instead" item', !adm.includes('disable-instead'));
  await closeMenu(page);
  for (const tab of ['measurements', 'calculated', 'setup', 'logs', 'history']) {
    await go(page, `#/devices/rtu1/${tab}`);
    await openMenu(page);
    check(`B the same menu on ${tab} (L3-UX-1)`, JSON.stringify(await menuIds(page)) === JSON.stringify(WANT_ADMIN), (await menuIds(page)).join());
    if (tab === 'measurements') {
      check('B on Measurements, E is Measurements…', (await page.getAttribute('#ddvActionsMenu [data-ddv-act="measurements"]', 'aria-keyshortcuts')) === 'E');
    }
    await closeMenu(page);
  }
  check('B the header: Test, Preview, Write… (disabled, said)', await page.locator('#ddvHeadTest').isVisible() && await page.locator('#ddvHeadPreview').isVisible()
        && (await page.getAttribute('#ddvHeadWrite', 'aria-disabled')) === 'true' && /Writes are off/.test(await page.getAttribute('#ddvHeadWrite', 'title') || ''));
  await go(page, '#/devices/tap1');
  await openMenu(page);
  check('B listen-only: Lock writes disabled, nothing is ever written (L3-UX-5)', (await item(page, 'lock').getAttribute('aria-disabled')) === 'true'
        && /Listen-only/.test(await why(page, 'lock')), await why(page, 'lock'));
  await closeMenu(page);
  await go(page, '#/devices/em1');
  await page.waitForSelector('#ddvUsedByBadge:not([hidden])');
  await openMenu(page);
  check('B used: Delete… held, "Used by", with Disable… beside it', (await item(page, 'delete').getAttribute('aria-disabled')) === 'true'
        && /Used by 1/.test(await why(page, 'delete')) && (await item(page, 'disable').getAttribute('aria-disabled')) !== 'true', await why(page, 'delete'));
  await closeMenu(page);
  await go(page, '#/devices/ups1');
  await openMenu(page);
  check('B meter-only: Polling… disabled, measured by its meters (L3-UX-9)', (await item(page, 'polling').getAttribute('aria-disabled')) === 'true'
        && /meters/.test(await why(page, 'polling')));
  await closeMenu(page);
  const op = await open('operator', { hash: '#/devices/rtu1' });
  await op.waitForSelector('#ddvActions');
  check('B operator: the ⋯ is there (L3-F13)', await op.locator('#ddvActions').isVisible());
  await openMenu(op);
  check('B operator: live actions, no configuration', JSON.stringify(await menuIds(op)) === '["test","preview","write","download-map","setup"]', (await menuIds(op)).join());
  check('B operator: nothing said "admin only" — role items are hidden', !/admin only/i.test(await op.locator('#ddvActionsMenu').innerText()));
  const vw = await open('viewer', { hash: '#/devices/rtu1' });
  await vw.waitForSelector('#ddvActions');
  check('B viewer: the ⋯ is there', await vw.locator('#ddvActions').isVisible());
  check('B viewer: no Test, no Write in the header', !(await vw.locator('#ddvHeadTest:visible, #ddvHeadWrite:visible').count()));
  await openMenu(vw);
  check('B viewer: what a viewer may do', JSON.stringify(await menuIds(vw)) === '["download-map","setup"]', (await menuIds(vw)).join());
  await vw.click('#ddvActionsMenu [data-ddv-act="setup"]');
  await vw.waitForTimeout(400);
  check('B viewer: Show settings opens Setup', (await activeTab(vw)) === 'setup');
  check('B viewer: Setup says who changes it, never "⋯ or E"', !/E key|⋯/.test(await vw.locator('.setup-intro').innerText()), await vw.locator('.setup-intro').innerText());
  await go(page, '#/devices/sunfield-u1');
  await openMenu(page);
  const um = await menuIds(page);
  check('B a unit: the same groups; what its installation sets says so', um.includes('open-installation') && um.includes('rename')
        && (await item(page, 'connection').getAttribute('aria-disabled')) === 'true' && /installation/i.test(await why(page, 'connection'))
        && (await item(page, 'delete').getAttribute('aria-disabled')) === 'true', um.join());
  await closeMenu(page);
  await go(page, '#/devices/dv-gx-battery-288');
  await openMenu(page);
  const cm = await menuIds(page);
  check('B a component: Remove…, Rename…, what its device sets says so', cm.includes('open-parent') && cm.includes('rename')
        && /Remove/.test(await item(page, 'delete').innerText()) && (await item(page, 'connection').getAttribute('aria-disabled')) === 'true', cm.join());
  await closeMenu(page);

  // a list row, a table row and a bridge line open the same builder
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  await page.waitForSelector('#devicesList [data-dev-menu="rtu1"]');
  await page.click('#devicesList .device-row[data-f="rtu1"] [data-dev-menu]');
  const rowMenu = '#devMenu-list-rtu1';
  await page.waitForSelector(`${rowMenu}:not([hidden])`);
  await page.waitForFunction(sel => !document.querySelector(`${sel} .is-checking`), rowMenu);
  const rm = await menuIds(page, rowMenu);
  check('B list row: the same menu', JSON.stringify(rm) === JSON.stringify(WANT_ADMIN), rm.join());
  check('B list row: no E (E works on the page)', await page.locator(`${rowMenu} [aria-keyshortcuts]`).count() === 0);
  check('B list row: the first item has the focus', await page.evaluate(sel => document.activeElement?.closest(sel) !== null, rowMenu));
  await page.keyboard.press('Escape');
  check('B list row: Esc gives the focus back to its ⋯', await page.evaluate(() => document.activeElement?.dataset.devMenu === 'rtu1'));
  // Disable… from a row asks what uses it (L3-UX-3): em1 is measured into ups1
  await page.click('#devicesList .device-row[data-f="em1"] [data-dev-menu]');
  await page.waitForSelector('#devMenu-list-em1:not([hidden])');
  const seen0 = page.dlg.seen.length;
  page.dlg.answers.push(false);
  await page.click('#devMenu-list-em1 [data-ddv-act="disable"]');
  await page.waitForTimeout(800);
  const asked = page.dlg.seen.slice(seen0).map(d => `${d.title} ${d.message || ''}`).join(' | ');
  check('B list row: Disable… names what uses it, never "nothing uses it"', /reacts/i.test(asked) && !/nothing uses it/i.test(asked), asked);
  await page.click('.bridge-card .bridge-dev[data-f="brpoll"] [data-dev-menu]');
  await page.waitForSelector('#devMenu-bridge-brpoll:not([hidden])');
  check('B bridge card: the same menu', (await menuIds(page, '#devMenu-bridge-brpoll')).includes('connection'));
  await page.click('#devMenu-bridge-brpoll [data-ddv-act="connection"]');
  await page.waitForSelector('#devEditModal.active #ddvConn');
  check('B bridge card: Edit connection… opens the device and its Edit modal at Connection',
        await page.locator('#ddvConn').isVisible() && /brpoll/.test(await page.evaluate(() => location.hash)));
  await closeDeviceEditor(page);
  await page.evaluate(() => { location.hash = '#/devices?view=list'; });
  await page.waitForSelector('.find-table [data-dev-menu="rtu1"]');
  await page.click('.find-table [data-dev-menu="rtu1"]');
  await page.waitForSelector('#devMenu-table-rtu1:not([hidden])');
  await page.waitForFunction(() => !document.querySelector('#devMenu-table-rtu1 .is-checking'));
  check('B table row: the same menu', JSON.stringify(await menuIds(page, '#devMenu-table-rtu1')) === JSON.stringify(WANT_ADMIN));
  await page.keyboard.press('Escape');
  await page.evaluate(() => { location.hash = '#/devices?view=tree'; });
  await page.evaluate(() => { location.hash = '#/installations/sunfield'; });
  await page.waitForSelector('[aria-controls="devMenu-installation-sunfield-u1"]');
  await page.click('[aria-controls="devMenu-installation-sunfield-u1"]');
  await page.waitForSelector('#devMenu-installation-sunfield-u1:not([hidden])');
  check('B installation unit row: the same menu as the unit\'s page', JSON.stringify(await menuIds(page, '#devMenu-installation-sunfield-u1')) === JSON.stringify(um),
        (await menuIds(page, '#devMenu-installation-sunfield-u1')).join());
  await page.keyboard.press('Escape');

  // ── C. the keyboard: menu button pattern ─────────────────────────────
  await go(page, '#/devices/rtu1');
  await page.focus('#ddvActions');
  await page.keyboard.press('Enter');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
  const f1 = await page.evaluate(() => document.activeElement?.dataset.ddvAct);
  await page.keyboard.press('ArrowDown');
  const f2 = await page.evaluate(() => document.activeElement?.dataset.ddvAct);
  await page.keyboard.press('End');
  const fEnd = await page.evaluate(() => document.activeElement?.dataset.ddvAct);
  await page.keyboard.press('ArrowDown');
  const fWrap = await page.evaluate(() => document.activeElement?.dataset.ddvAct);
  check('C Enter opens the menu on its first item', f1 === 'test', f1);
  check('C ↓ moves', f2 === 'preview', f2);
  check('C End: the last item (the restore line); ↓ wraps to the first', fEnd === 'restore' && fWrap === 'test', `${fEnd} ${fWrap}`);
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  check('C a disabled item keeps the focus (it is read, with its reason)', await page.evaluate(() => document.activeElement?.dataset.ddvAct) === 'write'
        && await page.evaluate(() => !!document.activeElement?.getAttribute('aria-describedby')));
  await page.keyboard.press('Escape');
  check('C Esc closes it and the focus is back on ⋯', await page.locator('#ddvActionsMenu').isHidden()
        && await page.evaluate(() => document.activeElement?.id === 'ddvActions'));

  // ── D. nothing edits in the foreground ───────────────────────────────
  for (const tab of ['overview', 'setup', 'measurements', 'calculated']) {
    await go(page, `#/devices/rtu1/${tab}`);
    await page.waitForTimeout(500);
    const saves = await visibleSaves(page);
    check(`D ${tab}: no Save in the page body`, saves.length === 0, saves.join());
  }
  await go(page, '#/devices/rtu1/measurements');
  check('D Measurements: what is read, not the catalog', await page.locator('#deviceDetailView [data-regtab="available"]').isHidden()
        && await page.locator('#deviceDetailView #selectedRegistersList').isVisible());
  await go(page, '#/devices/rtu1/setup');
  check('D Setup: no field to type in', await page.locator('[data-dpanel="setup"] input:visible, [data-dpanel="setup"] select:visible, [data-dpanel="setup"] textarea:visible').count() === 0);
  await go(page, '#/devices/dv-gx/commands');
  await page.waitForSelector('#deviceDetailView [data-dpanel="commands"] .cmd-card');
  const cmdPanel = page.locator('#deviceDetailView [data-dpanel="commands"]');
  check('D Commands: no Enabled tick, no editable binding field, no binding Save (Run and Test stay)',
        !(await cmdPanel.locator('[data-action="toggleDeviceCommand"]:visible').count())
        && !(await cmdPanel.locator('.cb-form input:not([disabled])').count()) && !(await cmdPanel.locator('[data-cb-save]').count())
        && await cmdPanel.locator('[data-action="runDeviceCommand"]').count() > 0);
  await go(page, '#/devices/dv-gx/overview');
  await page.waitForSelector('#devComponentsCard tr[data-comp]');
  check('D Overview: the components card renames, switches and removes nothing',
        !(await page.locator('#devComponentsCard [data-action="renameComponentUi"]:visible, #devComponentsCard [data-action="switchComponentUi"]:visible, #devComponentsCard [data-action="removeComponentUi"]:visible, #compAddForm:visible, #compFindBtn:visible').count()));
  await go(page, '#/devices/rtu1/calculated');
  check('D Calculated: no "From preset", no editor', !(await page.locator('#calcPresetSel:visible, #calcExpr:visible').count()));

  // ── E. editing from ⋯ and E ──────────────────────────────────────────
  // (L3.4: the Edit device modal — device_edit_modal_e2e.mjs covers it whole)
  const tabBefore = await activeTab(page);
  await openMenu(page);
  await page.click('#ddvActionsMenu [data-ddv-act="connection"]');
  await page.waitForSelector('#devEditModal.active #ddvConn');
  check('E Edit connection…: the Edit modal at Connection', await page.locator('#ddvConn').isVisible()
        && (await page.getAttribute('#devEditModal .dev-shell-tab[aria-selected="true"]', 'data-shell-sec')) === 'connection');
  check('E …its one Save is the modal\'s', await page.locator('#devEditSave').isVisible());
  check('E …the focus is on the section\'s heading', await page.evaluate(() => document.activeElement?.id === 'devEditH-connection'));
  await page.click('#devEditCancel');
  await page.waitForTimeout(300);
  check('E Cancel: back to the view as it was, no Save', (await activeTab(page)) === tabBefore && (await visibleSaves(page)).length === 0
        && !(await page.locator('#devEditModal.active').count()));
  check('E Cancel: the focus on ⋯, never the page', await page.evaluate(() => document.activeElement?.id === 'ddvActions'),
        await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 80)));
  await go(page, '#/devices/rtu1/measurements');
  await page.locator('#deviceDetailView h2').focus().catch(() => {});
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press('e');
  await page.waitForSelector('#ddvEditBar:not([hidden])');
  check('E the E key on Measurements opens the measurements editor (catalog, full screen, its one Save)',
        await page.locator('#deviceDetailView [data-regtab="available"]').isVisible() && await page.locator('#ddvEditSave').isVisible()
        && await page.locator('#deviceDetailView.ddv-full').count() === 1);
  await page.click('#ddvEditDone');
  await go(page, '#/devices/rtu1/overview');
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press('e');
  await page.waitForSelector('#devEditModal.active #ddvConn');
  check('E E elsewhere: the Edit modal at Connection', await page.locator('#ddvConn').isVisible());
  await closeDeviceEditor(page);
  await page.focus('#deviceDetailView h2 .ddv-name').catch(() => {});
  const vwE = await open('viewer', { hash: '#/devices/rtu1' });
  await vwE.waitForSelector('#ddvActions');
  await vwE.evaluate(() => document.activeElement?.blur());
  await vwE.keyboard.press('e');
  await vwE.waitForTimeout(400);
  check('E a viewer\'s E opens no editor', await vwE.locator('#ddvEditBar').isHidden() && !(await vwE.locator('#devEditModal.active').count()));
  await openMenu(page);
  await page.click('#ddvActionsMenu [data-ddv-act="outputs"]');
  await page.waitForSelector('#devEditModal.active #devEditOutputsHost .outputs-nav');
  check('E Outputs…: the Edit modal at Outputs', await page.locator('#devEditOutputsHost').isVisible());
  await closeDeviceEditor(page);
  await openMenu(page);
  await page.click('#ddvActionsMenu [data-ddv-act="about"]');
  await page.waitForSelector('#deviceDetailView .dev-attr-host .attr-card');
  check('E About…: the attributes editor', await page.locator('#deviceDetailView .dev-attr-host').isVisible());
  await page.click('#ddvEditDone');

  // ── K. leaving an editor never keeps, nor sends, what was typed ───────
  const stored = (await get('/api/devices/rtu1')).device.name;
  await go(page, '#/devices/rtu1');
  await openMenu(page);
  await page.click('#ddvActionsMenu [data-ddv-act="edit"]');
  await page.waitForSelector('#devEditModal.active #ddvConn');
  await editorSection(page, 'identity');
  await page.fill('#ddvName', 'Typed, never saved');
  const asked0 = page.dlg.seen.length;
  page.dlg.answers.push(false);
  await page.click('#devEditCancel');
  await page.waitForTimeout(500);
  check('K Cancel with typed edits asks first; "Keep editing" stays, the edit kept',
        page.dlg.seen.length === asked0 + 1 && /discard/i.test(page.dlg.seen[asked0].title || '')
        && await page.locator('#devEditModal.active').count() === 1 && (await page.inputValue('#ddvName')) === 'Typed, never saved');
  page.dlg.answers.push(true);
  await page.click('#devEditCancel');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
  await openMenu(page);
  await page.click('#ddvActionsMenu [data-ddv-act="edit"]');
  await page.waitForSelector('#devEditModal.active #ddvConn');
  await editorSection(page, 'identity');
  check('K …Discard drops them: the editor shows what is stored', (await page.inputValue('#ddvName')) === stored);
  // Reconnect sends what is stored, never what an editor held: typed, then
  // discarded (the modal never outlives a Reconnect from the page)
  await page.fill('#ddvName', 'Typed again');
  page.dlg.answers.push(true);
  await page.click('#devEditCancel');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
  const putBody = page.waitForRequest(r => r.method() === 'PUT' && new URL(r.url()).pathname === '/api/devices/rtu1');
  await page.evaluate(() => window.app.ddvReconnect('rtu1'));
  const sentName = (await putBody).postDataJSON()?.name;
  await page.waitForTimeout(800);
  check('K Reconnect sends the stored device, not the typed name', sentName === stored, sentName);
  check('K …and nothing typed was saved', (await get('/api/devices/rtu1')).device.name === stored);
  check('K the two Reconnect buttons have their own ids', await page.evaluate(() =>
    !document.querySelector('#deviceDetailView #ddvReconnect ~ #ddvReconnect') && document.querySelectorAll('#ddvReconnect').length <= 1));

  // ── L. the view keeps the live facts, for an operator and a viewer ────
  for (const role of ['operator', 'viewer']) {
    const lp = await open(role, { hash: '#/devices/em1/setup' });
    await lp.waitForSelector('[data-dpanel="setup"] [data-setup="outputs"] .sink-pill');
    const outText = await lp.locator('[data-dpanel="setup"] [data-setup="outputs"]').innerText();
    check(`L ${role}: each output says its live state`, (await lp.locator('[data-dpanel="setup"] [data-setup="outputs"] .sink-pill').count()) >= 4
          && /not connected|off in settings|active|off here|idle/i.test(outText), outText.slice(0, 200));
    check(`L ${role}: the REST URL without its key`, /apikey=••••/.test(outText) && !/zz9/.test(outText), outText.slice(0, 300));
    const testBtn = lp.locator('[data-dpanel="setup"] [data-action="ddvRestTest"]');
    check(`L ${role}: Test push ${role === 'operator' ? 'offered (a live action)' : 'not offered'}`,
          role === 'operator' ? await testBtn.isVisible() : !(await testBtn.isVisible()));
    if (role === 'operator') {
      const tested = lp.waitForResponse(r => /\/rest-push\/test$/.test(new URL(r.url()).pathname));
      await testBtn.click();
      check('L operator: Test push asks the gateway, saves nothing', (await tested).request().method() === 'POST');
    }
    await go(lp, '#/devices/dv-pq/setup');
    await lp.waitForSelector('[data-dpanel="setup"] [data-setup="pq"]');
    const pqText = await lp.locator('[data-dpanel="setup"] [data-setup="pq"]').innerText();
    check(`L ${role}: the PQ recorder's settings and its status line`, /300 s/.test(pqText)
          && await lp.locator('[data-dpanel="setup"] [data-setup="pq"] [data-pq-status]').count() === 1, pqText.slice(0, 200));
    await go(lp, '#/devices/ups1/setup');
    await lp.waitForSelector('[data-dpanel="setup"] [data-setup="meters"] .setup-gives .mtr-gives');
    const gives = await lp.locator('[data-dpanel="setup"] [data-setup="meters"] .setup-gives').innerText();
    check(`L ${role}: the meters say what they give, and why not`, /Loss|Efficiency/.test(gives) && /not|waiting|—/i.test(gives), gives.slice(0, 200));
    await go(lp, '#/devices/dv-web/setup');
    await lp.waitForSelector('[data-dpanel="setup"] [data-setup="connection"]');
    const webText = await lp.locator('[data-dpanel="setup"] [data-setup="connection"]').innerText();
    check(`L ${role}: an HTTP URL without its password or token`, !/s3cret|abc123/.test(webText) && /token=••••/.test(webText), webText.slice(0, 200));
    check(`L no page errors (${role})`, lp.errs.length === 0, lp.errs.slice(0, 2).join(' | '));
  }
  await go(page, '#/devices/dv-web/setup');
  await page.waitForSelector('[data-dpanel="setup"] [data-setup="connection"]');
  check('L admin: the HTTP URL masked for an admin too', !/s3cret|abc123/.test(await page.locator('[data-dpanel="setup"] [data-setup="connection"]').innerText()));
  // a link that promises an edit: the editor for an admin, Setup for the others
  await go(page, '#/devices/ups1/overview');
  await page.waitForSelector('#ddvPair a');
  await page.click('#ddvPair a');
  await page.waitForTimeout(600);
  check('L admin: the pair line\'s Meters link opens the meters editor', /Meters/.test(await page.locator('#ddvEditTitle').innerText())
        && await page.locator('#ddvEditBar').isVisible());
  const vl = await open('viewer', { hash: '#/devices/ups1/overview' });
  await vl.waitForSelector('#ddvPair a');
  await vl.click('#ddvPair a');
  await vl.waitForTimeout(800);
  check('L viewer: the same link opens Setup at Meters', (await activeTab(vl)) === 'setup'
        && await vl.evaluate(() => document.activeElement?.closest('[data-setup]')?.dataset.setup) === 'meters');

  // ── F. Setup, read-only, for every role ──────────────────────────────
  const vs = await open('viewer', { hash: '#/devices/rtu1/setup' });
  await vs.waitForSelector('[data-dpanel="setup"] [data-setup="connection"]');
  const heads = await vs.locator('[data-dpanel="setup"] section[data-setup] h3').allInnerTexts();
  check('F viewer: Setup sections with headings', ['Connection', 'Polling', 'Identity', 'Outputs'].every(h => heads.some(x => x.includes(h))), heads.join());
  const setupTxt = await vs.locator('[data-dpanel="setup"]').innerText();
  check('F viewer reads the line settings (L3-F16)', setupTxt.includes('9600 8N1') && setupTxt.includes('Master'), setupTxt.slice(0, 300));
  check('F a default says so', /\(default\)|default/.test(setupTxt));
  check('F viewer: no control in Setup', await vs.locator('[data-dpanel="setup"] input:visible, [data-dpanel="setup"] button:visible').count() === 0);
  await go(page, '#/devices/brpoll/setup');
  const locks = await page.locator('[data-dpanel="setup"] [data-setup="connection"] .setup-locks').innerText();
  check('F the locks with their reasons', /bridge 'panel'/.test(locks), locks);
  check('F every role: the Setup tab is not admin-only', await vs.locator('#dgrp-setup').isVisible());

  // ── G. links to the tabs that left ───────────────────────────────────
  for (const [old, sec] of [['connection', 'connection'], ['edit', 'connection'], ['outputs', 'outputs'],
                            ['attributes', 'identity'], ['meters', 'meters']]) {
    await go(vs, `#/devices/em1/${old}`);
    const where = await vs.evaluate(() => ({ tab: document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab,
      sec: document.activeElement?.closest('[data-setup]')?.dataset.setup }));
    check(`G #/devices/em1/${old} → Setup › ${sec}`, where.tab === 'setup' && where.sec === sec, JSON.stringify(where));
  }
  const groups = await vs.locator('#deviceWsGroups [role=tab]').evaluateAll(els => els.map(e => e.dataset.dgroup));
  check('G four groups: Live, Data, Setup, Diagnose', groups.join() === 'live,data,setup,diagnose', groups.join());
  check('G the groups are a tablist', (await vs.getAttribute('#deviceWsGroups', 'role')) === 'tablist');

  // ── H. a modal with unsaved changes asks (L3-F15) ────────────────────
  // (sections above asked too: H counts from here)
  const nDis = () => page.dlg.seen.filter(d => /discard/i.test(d.title)).length;
  const h0 = nDis();
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  await page.evaluate(() => window.app.openBridgeModal());
  await page.waitForSelector('#bridgeModal.active #brName');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  check('H an untouched modal closes on Esc, nothing asked', !(await page.locator('#bridgeModal.active').count())
        && nDis() === h0);
  await page.evaluate(() => window.app.openBridgeModal());
  await page.waitForSelector('#bridgeModal.active #brName');
  await page.fill('#brName', 'half typed');
  page.dlg.answers.push(false);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  check('H Esc on typed edits asks first (the page\'s dialog)', nDis() === h0 + 1, page.dlg.seen.map(d => d.title).join());
  check('H "Keep editing" keeps the modal and what was typed', await page.locator('#bridgeModal.active').count() === 1
        && (await page.inputValue('#brName')) === 'half typed');
  page.dlg.answers.push(true);
  await page.click('#bridgeModal .modal-close');
  await page.waitForTimeout(400);
  check('H ✕ asks too; Discard closes it', nDis() === h0 + 2
        && !(await page.locator('#bridgeModal.active').count()));
  // focus: Keep editing → the field typed in; Discard → the button that opened it
  await page.click('#devicesListView [data-action="openBridgeModal"]');
  await page.waitForSelector('#bridgeModal.active #brName');
  await page.fill('#brName', 'again');
  page.dlg.answers.push(false);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  check('H after Keep editing the focus is back in the field', await page.evaluate(() => document.activeElement?.id) === 'brName',
        await page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName));
  page.dlg.answers.push(true);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  check('H after Discard the focus is on what opened it', await page.evaluate(() => document.activeElement?.dataset.action) === 'openBridgeModal',
        await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 80)));
  // a field the page fills itself is not an edit; a form that redraws keeps the edit counted
  await page.evaluate(() => window.app.openBridgeModal());
  await page.waitForSelector('#bridgeModal.active #brName');
  await page.focus('#brName');
  await page.evaluate(() => { document.getElementById('brName').value = 'filled by the page'; });
  let before = nDis();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  check('H a field filled by the page after focus: Esc closes, nothing asked', nDis() === before && !(await page.locator('#bridgeModal.active').count()));
  await page.evaluate(() => window.app.openBridgeModal());
  await page.waitForSelector('#bridgeModal.active #brName');
  await page.fill('#brHost', '10.9.9.9');
  await page.evaluate(() => window.app._renderBridgeForm());
  before = nDis();
  page.dlg.answers.push(true);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  check('H a form that redrew itself still counts the edit: Esc asks', nDis() === before + 1 && !(await page.locator('#bridgeModal.active').count()));
  // Save closes without asking (Type…: the Edit modal at Identity, saved)
  await go(page, '#/devices/dv-gx');
  await openMenu(page);
  await page.click('#ddvActionsMenu #ddvSetKind');
  await page.waitForSelector('#devEditModal.active #devEditType');
  const pick = (await page.$$eval('#devEditType option', os => os.map(o => [o.value, o.selected]))).find(([v, s]) => v && !s);
  await page.selectOption('#devEditType', pick[0]);
  before = nDis();
  await saveDeviceEditor(page);
  await page.waitForTimeout(400);
  check('H Save closes the dialog without asking', nDis() === before && !(await page.locator('#devEditModal.active').count()));
  check('H no native dialog', page.dlg.native === 0);

  // ── I. a phone ───────────────────────────────────────────────────────
  const ph = await open('admin', { viewport: { width: 390, height: 844 }, hash: '#/devices/brpoll' });
  await ph.waitForSelector('#ddvFacts .dev-sum-text');
  await ph.waitForTimeout(500);
  for (const tab of ['overview', 'setup', 'measurements']) {
    await go(ph, `#/devices/brpoll/${tab}`);
    const wide = await ph.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(`I phone: ${tab} does not scroll sideways`, wide <= 0, `${wide}px`);
  }
  await openMenu(ph);
  const box = await ph.locator('#ddvActionsMenu').boundingBox();
  check('I phone: the menu is a bottom sheet that fits the screen (L3-UX-10)', box && box.x >= 0 && box.x + box.width <= 390
        && box.y >= 0 && Math.round(box.y + box.height) <= 844 && Math.round(box.y + box.height) >= 838, JSON.stringify(box));
  await ph.locator('#ddvActionsMenu [data-ddv-act="restore"]').scrollIntoViewIfNeeded();
  const last = await ph.locator('#ddvActionsMenu [data-ddv-act="restore"]').boundingBox();
  check('I phone: its last item can be reached on screen', last && last.y + last.height <= 844 && last.y >= 0, JSON.stringify(last));
  await closeMenu(ph);

  // ── J. Romanian ──────────────────────────────────────────────────────
  const ro = await open('admin', { lang: 'ro', hash: '#/devices/rtu1/setup' });
  await ro.waitForSelector('[data-dpanel="setup"] [data-setup="connection"]');
  const roGroups = await ro.locator('#deviceWsGroups [role=tab]').allInnerTexts();
  check('J ro: the groups', roGroups.join('|') === 'Live|Date|Configurare|Diagnostic', roGroups.join('|'));
  check('J ro: Setup headings', /Conexiune/.test(await ro.locator('[data-dpanel="setup"] [data-setup="connection"] h3').innerText()));
  await openMenu(ro);
  check('J ro: the menu', /Editează dispozitivul/.test(await ro.locator('#ddvActionsMenu [data-ddv-act="edit"]').innerText()));
  check('J ro: a reason in Romanian', /Scrierile sunt oprite/.test(await ro.locator('#ddvActionsMenu [data-ddv-act="write"] .mbg-menu-why').innerText()));

  for (const p of [page, op, vw, vwE, vs, ph, ro]) check(`no page errors (${p.url().split('#')[1] || '/'})`, p.errs.length === 0, p.errs.slice(0, 2).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e.stack || e).slice(0, 600));
} finally {
  await cleanup().catch(() => {});
  for (const c of pages) await c.close().catch(() => {});
  await browser.close();
  const f = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - f}/${results.length} passed`);
  process.exit(f ? 1 : 0);
}
