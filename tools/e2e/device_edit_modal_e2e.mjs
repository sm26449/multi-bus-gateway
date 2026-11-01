// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The Edit device modal (ADR-0031 §4, L3.4), as an administrator and the
// other roles meet it:
// A  it opens from ⋯ (Edit…, Edit connection…, Polling…, Type…, Outputs…) and
//    from E, at the section asked for, the focus on its heading (Type…: on
//    the type); Cancel closes, the focus back on ⋯
// B  a rename and an output switch: one PATCH, "Save", applied live
// C  a unit id: "Save & restart acquisition", and acquisition restarts
// D  Master → Listen-only: a review in the page's own dialog before Save;
//    Cancel there saves nothing; an output going off is reviewed too
// E  two tabs: the second Save answers 412 — the edits kept, re-applied to
//    the version stored now, the field both changed marked; Save again saves
// F  the dirty guard: Esc and ✕ ask; Keep editing keeps; Discard drops
// G  keyboard only: E, the rail's arrows, the fields, Save
// H  a phone (390 px): full screen, the rail a list to pick from, nothing
//    scrolls sideways
// I  Romanian
// J  a viewer and an operator cannot open it
// K  a meter-only device: no Model nor Connection; a unit's Outputs… is the
//    modal too, its installation's outputs (L3.11b)
// L  locks: the reasons listed; a locked field aria-disabled, focusable, kept
// N  a Save in flight: the sections inert, Esc held; a value set meanwhile
//    keeps the dialog open over what was saved
// O  a page editor with unsaved edits, then the dialog: asked first
// P  Back with unsaved edits asks; beforeunload; a save refused after the
//    dialog closed is said
// Q  OPC UA "only the values I choose" with none ticked: refused here
// R  a bus move on the server's field names (serial_port, bridge, host
//    under rtu-tcp): reviewed, named
// S  a phone: picking a section keeps the focus on the list
// M  Upload map assigns the template by the Model section alone: a
//    connection saved meanwhile is kept (L3-F11)
//
// Against a throwaway gateway of the qa fixture, login on:
//   MBG_FX_NAME=mbg-fx-edit tools/e2e/fixture_up.sh qa 18341 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18341 MBG_PASS=e2e-pass node tools/e2e/device_edit_modal_e2e.mjs
//   docker rm -f mbg-fx-edit
// It edits em1, rtu1 and ups1 and puts back what it changed.
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18341';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : ''}`);
};

const api = await pwRequest.newContext({ baseURL: BASE });
const login = await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const dev = async id => (await (await api.get(`/api/devices/${id}`)).json()).device;
const etag = async id => (await api.get(`/api/devices/${id}`)).headers().etag;
async function patch(id, sections) {
  return api.patch(`/api/devices/${id}`, { data: { sections }, headers: { 'If-Match': await etag(id) } });
}
const before = {};
async function remember() {
  for (const id of ['em1', 'rtu1', 'ups1']) before[id] = await dev(id);
  before.em1Groups = (await (await api.get('/api/devices/em1/poll-groups')).json()).poll_groups;
}
async function restore() {
  const em1 = before.em1, rtu1 = before.rtu1;
  const groups = Object.entries(before.em1Groups || {}).map(([k, g]) => [k, { interval: g.interval }]);
  if (em1) await patch('em1', { identity: { name: em1.name }, connection: { unit_id: em1.connection.unit_id, protocol: 'tcp' },
                                outputs: { mqtt: true, http_output: !!em1.http_output_enabled },
                                ...(groups.length ? { polling: { poll_groups: Object.fromEntries(groups) } } : {}) });
  if (rtu1) await patch('rtu1', { identity: { name: rtu1.name }, outputs: { mqtt: true } });
  if (before.ups1) await patch('ups1', { identity: { name: before.ups1.name } });
  await api.delete('/api/devices/e2e-rtu2');
  await api.delete('/api/devices/e2e-rtutcp');
  await api.delete('/api/device-templates/e2e_sdm120_copy');
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const contexts = [];
async function open(role, opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  contexts.push(ctx);
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|412/.test(m.text())) page.errs.push(m.text()); });
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
const go = async (page, id) => {
  await page.evaluate(h => { location.hash = h; }, `#/devices/${id}`);
  await page.waitForSelector('#deviceDetailView #ddvFacts', { state: 'visible' });
  await page.waitForFunction(i => window.app?._devDetail?.id === i, id);
  await page.waitForTimeout(500);
};
async function menu(page, item) {
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
  await page.click(`#ddvActionsMenu [data-ddv-act="${item}"]`);
}
const modalOpen = page => page.locator('#devEditModal.active').count().then(n => n === 1);
async function waitOpen(page) {
  await page.waitForSelector('#devEditModal.active .dev-shell-panel:not([hidden])');
  await page.waitForTimeout(150);
}
const selected = page => page.getAttribute('#devEditModal .dev-shell-tab[aria-selected="true"]', 'data-shell-sec');
const active = page => page.evaluate(() => document.activeElement?.id || '');
const summary = page => page.locator('#devEditSummary').innerText();
const saveLabel = page => page.locator('#devEditSave').innerText();
// the dry run settled: the footer no longer says it is checking
async function settled(page) {
  await page.waitForTimeout(450);
  await page.waitForFunction(() => !/Checking|Se verifică/.test(document.getElementById('devEditSummary')?.textContent || ''));
}
async function cancel(page) {
  await page.click('#devEditCancel');
  await page.waitForSelector('#devEditModal.active', { state: 'detached' }).catch(() => {});
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
}
async function pick(page, sec) {
  await page.click(`#devEditModal [role=tab][data-shell-sec="${sec}"]`);
  await page.waitForSelector(`#devEditPanel-${sec}:not([hidden])`);
}
async function save(page) {
  const resp = page.waitForResponse(r => r.request().method() === 'PATCH' && /\/api\/devices\/[^/?]+$/.test(new URL(r.url()).pathname)
                                       && !/dry_run/.test(r.url()));
  await page.click('#devEditSave');
  return resp;
}

try {
  check('logged in (the fixture\'s login)', login.ok());
  await remember();
  const page = await open('admin');

  // ── A. where it opens ────────────────────────────────────────────────
  await go(page, 'em1');
  for (const [item, sec] of [['connection', 'connection'], ['polling', 'polling'], ['outputs', 'outputs'], ['type', 'identity']]) {
    await menu(page, item);
    await waitOpen(page);
    check(`A ⋯ → ${item}: the modal at ${sec}`, (await selected(page)) === sec, await selected(page));
    const want = item === 'type' ? 'devEditType' : `devEditH-${sec}`;
    check(`A …the focus on ${want}`, (await active(page)) === want, await active(page));
    await cancel(page);
    check(`A …Cancel (nothing changed) closes, the focus back on ⋯`, !(await modalOpen(page)) && (await active(page)) === 'ddvActions', await active(page));
  }
  // E on the Overview: the connection; on Setup › Identity: Identity
  await page.keyboard.press('e');
  await waitOpen(page);
  check('A E on the Overview: the modal at Connection', (await selected(page)) === 'connection');
  check('A …a dialog, named, modal', await page.evaluate(() => {
    const d = document.querySelector('#devEditModal .modal-content');
    return d.getAttribute('role') === 'dialog' && d.getAttribute('aria-modal') === 'true'
      && /Edit/.test(document.getElementById(d.getAttribute('aria-labelledby'))?.textContent || '');
  }));
  check('A …a vertical tablist, five sections in order', await page.evaluate(() => {
    const r = document.querySelector('#devEditModal [role=tablist]');
    return r.getAttribute('aria-orientation') === 'vertical'
      && [...r.querySelectorAll('[role=tab]')].map(b => b.dataset.shellSec).join() === 'model,connection,polling,identity,outputs';
  }));
  check('A …no change: "No changes", Save held back', /No changes/.test(await summary(page))
        && (await page.getAttribute('#devEditSave', 'aria-disabled')) === 'true');
  await cancel(page);
  await page.click('#deviceWsGroups [data-dgroup="setup"]');
  await page.waitForSelector('[data-dpanel="setup"] [data-setup="identity"]');
  await page.focus('#setupH-identity');
  await page.evaluate(() => { document.activeElement.blur(); window.app._setupSection = 'identity'; });
  await page.keyboard.press('e');
  await waitOpen(page);
  check('A E on Setup › Identity: the modal at Identity', (await selected(page)) === 'identity', await selected(page));
  await cancel(page);
  await page.click('#deviceWsGroups [data-dgroup="live"]');

  // ── B. a rename and an output: "Save", applied live ──────────────────
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'Cabinet meter (e2e)');
  await pick(page, 'outputs');
  await page.click('#outTab-http');
  await page.waitForSelector('#outDetail');
  const httpWas = await page.isChecked('#outEnabled');
  await page.click('#outEnabled');
  await settled(page);
  check('B the footer: 2 changes · applies live', /^2 changes · applies live$/.test(await summary(page)), await summary(page));
  check('B the button says "Save"', (await saveLabel(page)).trim() === 'Save', await saveLabel(page));
  check('B the rail marks Identity and Outputs as changed', await page.evaluate(() =>
    ['identity', 'outputs'].every(s => !document.querySelector(`#devEditTab-${s} .dev-shell-dot`).hidden)
    && document.querySelector('#devEditTab-model .dev-shell-dot').hidden
    && document.getElementById('devEditTab-identity').getAttribute('aria-describedby') === 'devEditTab-identity-changed'));
  await page.click('#devEditShowChanges');
  const diffTxt = await page.locator('#devEditDiff').innerText();
  check('B Show changes lists each change, stored → after', /name/.test(diffTxt) && /Cabinet meter \(e2e\)/.test(diffTxt) && /http_output/.test(diffTxt), diffTxt);
  check('B the switch was staged, nothing sent', (await dev('em1')).name === before.em1.name);
  const rB = await save(page);
  const bB = rB.request().postDataJSON();
  const aB = await rB.json();
  check('B one PATCH with If-Match, both sections', rB.status() === 200 && !!rB.request().headers()['if-match']
        && bB.sections.identity.name === 'Cabinet meter (e2e)' && bB.sections.outputs.http_output === !httpWas, JSON.stringify(bB));
  check('B applied live (no restart)', aB.restarted === false, aB);
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
  const em1B = await dev('em1');
  check('B stored', em1B.name === 'Cabinet meter (e2e)' && em1B.http_output_enabled === !httpWas);
  await page.waitForTimeout(600);
  check('B the page shows it, the focus on ⋯', /Cabinet meter \(e2e\)/.test(await page.locator('#deviceDetailView .ddv-name').first().innerText())
        && (await active(page)) === 'ddvActions', await active(page));

  // ── C. a unit id: "Save & restart acquisition" ───────────────────────
  await menu(page, 'connection');
  await waitOpen(page);
  const unitWas = Number(await page.inputValue('#ddvUnit'));
  await page.fill('#ddvUnit', String(unitWas === 7 ? 8 : 7));
  await settled(page);
  check('C the footer: restarts acquisition', /1 change · restarts acquisition/.test(await summary(page)), await summary(page));
  check('C the button says so', (await saveLabel(page)).trim() === 'Save & restart acquisition', await saveLabel(page));
  const rC = await save(page);
  const aC = await rC.json();
  check('C saved, acquisition restarted', rC.status() === 200 && aC.restarted === true, aC);
  check('C stored', (await dev('em1')).connection.unit_id === (unitWas === 7 ? 8 : 7));
  check('C no native dialog, no page error', page.dlg.native === 0 && page.errs.length === 0, page.errs.join(' | '));

  // ── D. reviews: Master → Listen-only, an output off ──────────────────
  await go(page, 'rtu1');
  await menu(page, 'connection');
  await waitOpen(page);
  await page.click('label:has(#ddvRole-listen)');
  await settled(page);
  const nD = page.dlg.seen.length;
  page.dlg.answers.push(false);
  await page.click('#devEditSave');
  await page.waitForTimeout(1200);
  const reviewD = page.dlg.seen[nD];
  check('D Master → Listen-only: a review first, in the page\'s dialog', !!reviewD && reviewD.kind === 'confirm'
        && /Review/.test(reviewD.title) && reviewD.details.some(x => /Master → Listen-only/.test(x)), reviewD);
  check('D …Cancel there saves nothing; the modal stays with the edit', (await dev('rtu1')).protocol === 'rtu' && await modalOpen(page)
        && await page.isChecked('#ddvRole-listen'));
  await page.click('label:has(#ddvRole-poll)');
  await pick(page, 'outputs');
  await page.click('#outTab-mqtt');
  await page.waitForSelector('#outDetail');
  await page.uncheck('#outEnabled');
  await settled(page);
  page.dlg.answers.push(true);
  const rD = await save(page);
  const reviewD2 = page.dlg.seen[page.dlg.seen.length - 1];
  check('D an output going off is reviewed (MQTT)', /Output off/.test((reviewD2.details || []).join()) , reviewD2);
  check('D …confirmed: saved, MQTT off', rD.status() === 200 && (await dev('rtu1')).mqtt_enabled === false);
  // a rename alone is never reviewed
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'RTU one (e2e)');
  await settled(page);
  const nD3 = page.dlg.seen.length;
  const rD3 = await save(page);
  check('D a rename saves without a review', rD3.status() === 200 && page.dlg.seen.length === nD3);
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));

  // ── E. two tabs: 412 — the edits kept and re-applied ─────────────────
  await go(page, 'em1');
  const other = await open('admin', { hash: '#/devices/em1' });
  await other.waitForSelector('#ddvFacts .dev-sum-text');
  await other.waitForTimeout(500);
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'Mine (tab 1)');
  await pick(page, 'polling');
  const pg = page.locator('#devEditPanel-polling .ddv-pg').first();
  const pgId = await pg.getAttribute('id');
  const pgWas = await pg.inputValue();
  await pg.fill(String(Number(pgWas) + 1));
  await settled(page);
  // meanwhile, in the other tab: a rename, saved
  await menu(other, 'edit');
  await waitOpen(other);
  await pick(other, 'identity');
  await other.fill('#ddvName', 'Theirs (tab 2)');
  await settled(other);
  const rE0 = await save(other);
  check('E the other tab saved first', rE0.status() === 200);
  // Save in the first: 412
  await page.click('#devEditSave');
  await page.waitForSelector('#devEditConflict .dev-shell-conflictbox');
  const banner = await page.locator('#devEditConflict').innerText();
  check('E 412: a banner, role=alert: changed elsewhere, edits kept, nothing saved', /changed elsewhere/.test(banner) && /Nothing was saved/.test(banner)
        && (await page.getAttribute('#devEditConflict', 'role')) === 'alert', banner);
  await pick(page, 'identity');
  check('E …my name kept', (await page.inputValue('#ddvName')) === 'Mine (tab 1)');
  const note = await page.locator('#ddvName-kept').innerText().catch(() => '');
  check('E …marked: "Stored now: Theirs (tab 2)"', /Stored now: Theirs \(tab 2\)/.test(note), note);
  await pick(page, 'polling');
  check('E …my interval re-applied (not a conflict: only I changed it)', (await page.inputValue(`#${pgId}`)) === String(Number(pgWas) + 1)
        && !(await page.locator(`#${pgId}-kept`).count()));
  check('E …not saved', (await dev('em1')).name === 'Theirs (tab 2)');
  await settled(page);
  const rE = await save(page);
  check('E Save again: saved over the new version', rE.status() === 200 && (await dev('em1')).name === 'Mine (tab 1)');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
  await other.close();

  // ── F. the dirty guard ───────────────────────────────────────────────
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'Typed, never saved');
  let n0 = page.dlg.seen.length;
  page.dlg.answers.push(false);                       // Keep editing
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  check('F Esc with an edit asks first (Discard your changes?)', page.dlg.seen.length === n0 + 1
        && /Discard/.test(page.dlg.seen[n0].title) && await modalOpen(page));
  check('F …Keep editing keeps the edit', (await page.inputValue('#ddvName')) === 'Typed, never saved');
  n0 = page.dlg.seen.length;
  page.dlg.answers.push(true);                        // Discard
  await page.click('#devEditClose');
  await page.waitForTimeout(600);
  check('F ✕ asks too; Discard closes', page.dlg.seen.length === n0 + 1 && !(await modalOpen(page)));
  check('F …nothing was sent', (await dev('em1')).name === 'Mine (tab 1)');
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  check('F …reopened: what is stored', (await page.inputValue('#ddvName')) === 'Mine (tab 1)');
  n0 = page.dlg.seen.length;
  await cancel(page);
  check('F Cancel with nothing changed asks nothing', page.dlg.seen.length === n0);

  // ── G. keyboard only ─────────────────────────────────────────────────
  await page.keyboard.press('e');
  await waitOpen(page);
  // into the rail (the selected tab is its one stop): Shift+Tab from the heading
  await page.keyboard.press('Shift+Tab');
  check('G Shift+Tab from the heading: the rail\'s selected tab', (await active(page)) === 'devEditTab-connection', await active(page));
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  check('G the rail: arrows move and select (Identity)', (await selected(page)) === 'identity' && (await active(page)) === 'devEditTab-identity');
  await page.keyboard.press('End');
  check('G …End: Outputs', (await selected(page)) === 'outputs');
  await page.keyboard.press('Home');
  check('G …Home: Model', (await selected(page)) === 'model');
  await page.keyboard.press('ArrowUp');
  check('G …wraps (Up from the first: the last)', (await selected(page)) === 'outputs');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Tab');
  check('G Tab from the rail: into the section', (await active(page)) === 'ddvName', await active(page));
  await page.keyboard.press('Control+A');
  await page.keyboard.type('Keyboard meter');
  await settled(page);
  let hops = 0;
  while ((await active(page)) !== 'devEditSave' && hops++ < 40) await page.keyboard.press('Tab');
  check('G Tab reaches Save', (await active(page)) === 'devEditSave');
  const rG = page.waitForResponse(r => r.request().method() === 'PATCH' && !/dry_run/.test(r.url()));
  await page.keyboard.press('Enter');
  check('G Enter on Save saves', (await rG).status() === 200 && (await dev('em1')).name === 'Keyboard meter');

  // ── H. a phone ───────────────────────────────────────────────────────
  const ph = await open('admin', { viewport: { width: 390, height: 844 }, hash: '#/devices/em1' });
  await ph.waitForSelector('#ddvFacts .dev-sum-text');
  await ph.waitForTimeout(500);
  await menu(ph, 'outputs');
  await waitOpen(ph);
  const box = await ph.locator('#devEditModal .modal-content').boundingBox();
  check('H full screen', box && box.width >= 389 && box.height >= 800, box);
  check('H the rail is a list to pick from (a select), the tabs hidden', await ph.locator('#devEditRailSel').isVisible()
        && !(await ph.locator('#devEditRail').isVisible()));
  await ph.selectOption('#devEditRailSel', 'identity');
  check('H picking a section shows it', await ph.locator('#devEditPanel-identity').isVisible() && (await selected(ph)) === 'identity');
  check('H nothing scrolls sideways', await ph.evaluate(() => {
    const m = document.querySelector('#devEditModal .dev-shell-main');
    return document.documentElement.scrollWidth <= innerWidth && m.scrollWidth <= m.clientWidth + 1;
  }));
  const sb = await ph.locator('#devEditSave').boundingBox();
  check('H Save stays on screen (sticky footer)', sb && sb.y + sb.height <= 844 && sb.x + sb.width <= 390, sb);
  await ph.close();

  // ── I. Romanian ──────────────────────────────────────────────────────
  const ro = await open('admin', { lang: 'ro', hash: '#/devices/em1' });
  await ro.waitForSelector('#ddvFacts .dev-sum-text');
  await ro.waitForTimeout(500);
  await menu(ro, 'connection');
  await waitOpen(ro);
  check('I ro: the title and the sections', /Editează/.test(await ro.locator('#devEditTitle').innerText())
        && /Conexiune/.test(await ro.locator('#devEditTab-connection').innerText()) && /Ieșiri/.test(await ro.locator('#devEditTab-outputs').innerText()));
  check('I ro: no changes', /Nicio modificare/.test(await summary(ro)), await summary(ro));
  const u = Number(await ro.inputValue('#ddvUnit'));
  await ro.fill('#ddvUnit', String(u + 1));
  await settled(ro);
  check('I ro: the restart said, the button too', /repornește achiziția/.test(await summary(ro)) && /Salvează și repornește achiziția/.test(await saveLabel(ro)),
        `${await summary(ro)} | ${await saveLabel(ro)}`);
  ro.dlg.answers.push(true);
  await ro.keyboard.press('Escape');
  await ro.waitForTimeout(500);
  check('I ro: the discard question in Romanian', /Renunțați|modificări/i.test(ro.dlg.seen[ro.dlg.seen.length - 1]?.title || ''), ro.dlg.seen.slice(-1));
  await ro.close();

  // ── J. viewer and operator ───────────────────────────────────────────
  for (const role of ['viewer', 'operator']) {
    const vp = await open(role, { hash: '#/devices/em1' });
    await vp.waitForSelector('#ddvFacts .dev-sum-text');
    await vp.waitForTimeout(500);
    await vp.click('#ddvActions');
    const items = await vp.locator('#ddvActionsMenu [role=menuitem]').evaluateAll(els => els.filter(e => e.offsetParent).map(e => e.dataset.ddvAct));
    check(`J ${role}: no editing item in ⋯`, !items.some(i => ['edit', 'connection', 'polling', 'outputs', 'type'].includes(i)), items);
    await vp.keyboard.press('Escape');
    await vp.evaluate(() => document.activeElement.blur());
    await vp.keyboard.press('e');
    await vp.waitForTimeout(500);
    const forced = await vp.evaluate(() => window.app.devEditOpen('connection'));
    await vp.waitForTimeout(300);
    check(`J ${role}: E and a direct call open nothing`, !(await modalOpen(vp)) && forced === false);
    await vp.close();
  }

  // ── K. kinds ─────────────────────────────────────────────────────────
  await go(page, 'ups1');
  await menu(page, 'edit');
  await waitOpen(page);
  const secsK = await page.locator('#devEditModal .dev-shell-tab').evaluateAll(els => els.map(e => e.dataset.shellSec));
  check('K meter-only: Polling · Identity · Outputs (no Model nor Connection)', secsK.join() === 'polling,identity,outputs', secsK);
  check('K …Edit… opens at Identity (it has no connection)', (await selected(page)) === 'identity');
  await page.fill('#ddvName', 'UPS (e2e)');
  await settled(page);
  const rK = await save(page);
  check('K …a rename saves', rK.status() === 200 && (await dev('ups1')).name === 'UPS (e2e)');
  await go(page, 'sunfield-u1');
  await menu(page, 'outputs');
  await waitOpen(page);
  check('K a unit: Outputs… is the Edit device modal, its installation\'s outputs, said so (L3.11b)',
        await modalOpen(page) && await page.locator('#devEditOutputsHost .outputs-nav').isVisible()
        && /every unit/.test(await page.locator('#devEditOutputsShared').innerText()));
  await page.click('#devEditCancel');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));

  // ── L. locks ─────────────────────────────────────────────────────────
  await go(page, 'em1');
  await menu(page, 'connection');
  await waitOpen(page);
  const lockTxt = await page.locator('#devEditPanel-connection .dev-shell-locks').innerText();
  check('L the reasons listed (protocol fixed, role: polled by em2)', /connection\.protocol/.test(lockTxt) && /connection\.role/.test(lockTxt) && /em2/.test(lockTxt), lockTxt);
  await cancel(page);
  await go(page, 'brpoll');
  await menu(page, 'connection');
  await waitOpen(page);
  const lockB = await page.locator('#devEditPanel-connection .dev-shell-locks').innerText();
  check('L a bridged device: its line is the bridge\'s', /connection\.line/.test(lockB) && /bridge/.test(lockB), lockB);
  await cancel(page);
  // a second device on rtu1's serial port: the bus owns the line
  const mk = await api.post('/api/devices', { data: { id: 'e2e-rtu2', name: 'E2E second on QA1', template: 'eastron_sdm120', enabled: false,
    connection: { protocol: 'rtu', serial_port: '/dev/ttyQA1', baudrate: 9600, parity: 'N', stopbits: 1, bytesize: 8, unit_id: 5 } } });
  check('L (a second device on /dev/ttyQA1)', mk.ok(), await mk.text());
  await go(page, 'rtu1');
  await menu(page, 'connection');
  await waitOpen(page);
  const lockS = await page.locator('#devEditPanel-connection .dev-shell-locks').innerText();
  check('L a shared serial bus: the line is the bus\'s, said', /connection\.line/.test(lockS) && /e2e-rtu2/.test(lockS), lockS);
  const baud = page.locator('#ddvBaud');
  check('L …the line fields aria-disabled, focusable, described by the reason',
        (await baud.getAttribute('aria-disabled')) === 'true' && await baud.evaluate(el => !el.disabled && el.tabIndex >= 0)
        && /devEditLock-/.test(await baud.getAttribute('aria-describedby') || ''));
  const baudWas = await baud.inputValue();
  await baud.focus();
  await page.keyboard.press('ArrowDown');           // a user's change (keyboard)
  await baud.evaluate(el => { el.value = el.value === '19200' ? '9600' : '19200'; el.dispatchEvent(new Event('change', { bubbles: true })); });
  await page.waitForTimeout(500);
  check('L …and keep their value', (await baud.inputValue()) === baudWas && /No changes/.test(await summary(page)), await baud.inputValue());
  await cancel(page);
  await api.delete('/api/devices/e2e-rtu2');
  check('L no native dialog, no page error (admin)', page.dlg.native === 0 && page.errs.length === 0, page.errs.join(' | '));

  // ── N. a Save in flight: nothing can be typed, nothing typed is lost ──
  await go(page, 'em1');
  const nameN = (await dev('em1')).name;
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'Busy meter (e2e)');
  await settled(page);
  let releaseN;
  const heldN = new Promise(res => { releaseN = res; });
  await page.route(/\/api\/devices\/em1$/, async r => {
    if (r.request().method() === 'PATCH') await heldN;
    await r.continue();
  });
  const sentN = page.waitForRequest(r => r.method() === 'PATCH' && /\/api\/devices\/em1$/.test(r.url()));
  await page.click('#devEditSave');
  await sentN;
  check('N in flight: the sections are inert, Save and Cancel held back',
        await page.evaluate(() => document.getElementById('devEditMain').inert === true)
        && (await page.getAttribute('#devEditSave', 'aria-disabled')) === 'true'
        && (await page.getAttribute('#devEditCancel', 'aria-disabled')) === 'true');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  check('N in flight: Esc does not close it', await modalOpen(page));
  // a value set while it saves (past the inert sections: the guard)
  await page.evaluate(() => window.app._deSet('identity.name', 'Set while saving'));
  releaseN();
  await page.waitForSelector('#devEditConflict .dev-shell-conflictbox');
  await page.unroute(/\/api\/devices\/em1$/);
  check('N the value set while saving: the dialog stays, says it was saved, keeps the new value',
        await modalOpen(page) && /Saved\./.test(await page.locator('#devEditConflict').innerText())
        && (await page.inputValue('#ddvName')) === 'Set while saving' && (await dev('em1')).name === 'Busy meter (e2e)');
  check('N …and it is not inert any more', await page.evaluate(() => !document.getElementById('devEditMain').inert));
  page.dlg.answers.push(true);
  await cancel(page);
  await patch('em1', { identity: { name: nameN } });

  // ── O. a page editor with unsaved edits, then the dialog: asked first ──
  await go(page, 'ups1');
  await menu(page, 'meters');
  await page.waitForSelector('#mtrRole-0');
  // a select of the meters editor with another value to pick
  let mSel = '', mWas = '', mTo = '';
  for (const sel of ['#mtrPower-0', '#mtrEnergy-0', '#mtrRole-0']) {
    const vals = await page.locator(`${sel} option`).evaluateAll(os => os.filter(o => !o.disabled).map(o => o.value));
    const was = await page.inputValue(sel);
    const to = vals.find(v => v !== was);
    if (to !== undefined) { mSel = sel; mWas = was; mTo = to; break; }
  }
  await page.selectOption(mSel, mTo);
  await page.locator('#mtrDev-0-pick').fill('zz typed');   // typed in its device picker too
  let o0 = page.dlg.seen.length;
  page.dlg.answers.push(false);                       // Keep editing
  await page.evaluate(() => document.activeElement.blur());
  await menu(page, 'type');
  await page.waitForTimeout(600);
  check('O the meters editor has unsaved edits: Type… asks first; Keep editing stays there',
        page.dlg.seen.length === o0 + 1 && /Discard/.test(page.dlg.seen[o0].title) && !(await modalOpen(page))
        && await page.locator('#ddvEditBar').isVisible() && await page.evaluate(() => window.app._ddvEditDirty()));
  page.dlg.answers.push(true);                        // Discard
  await menu(page, 'type');
  await waitOpen(page);
  check('O …Discard: the dialog opens, the page editor is gone (drawn from what is stored)',
        await modalOpen(page) && await page.locator('#ddvEditBar').isHidden());
  await cancel(page);

  // ── P. leaving the page with unsaved edits: asked; a failed save said ──
  await go(page, 'em1');
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'Typed, then Back');
  o0 = page.dlg.seen.length;
  page.dlg.answers.push(false);                       // Keep editing
  await page.goBack();
  await page.waitForTimeout(800);
  check('P Back with unsaved edits asks first; Keep editing: still here, the address back',
        page.dlg.seen.length === o0 + 1 && /Discard/.test(page.dlg.seen[o0].title) && await modalOpen(page)
        && (await page.evaluate(() => location.hash)) === '#/devices/em1' && (await page.inputValue('#ddvName')) === 'Typed, then Back');
  // the browser's own question on a reload/close
  const unload = [];
  page.on('dialog', d => { if (d.type() === 'beforeunload') unload.push(d.type()); });
  check('P a reload or close asks too (beforeunload)', await page.evaluate(() => {
    const ev = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(ev);
    return ev.defaultPrevented;
  }));
  page.dlg.answers.push(true);                        // Discard
  await page.evaluate(() => { location.hash = '#/devices'; });
  await page.waitForTimeout(1000);
  check('P …Discard: the route goes on', !(await modalOpen(page)) && /#\/devices$/.test(await page.evaluate(() => location.hash)),
        JSON.stringify({ modal: await modalOpen(page), hash: await page.evaluate(() => location.hash), seen: page.dlg.seen.slice(-2).map(d => [d.title, d.answer]) }));
  check('P …nothing typed was saved', (await dev('em1')).name === nameN);
  // a save sent, then the page left, then the save refused: said
  await go(page, 'em1');
  await menu(page, 'edit');
  await waitOpen(page);
  await pick(page, 'identity');
  await page.fill('#ddvName', 'Refused later');
  await settled(page);
  let releaseP;
  const heldP = new Promise(res => { releaseP = res; });
  await page.route(/\/api\/devices\/em1$/, async r => {
    if (r.request().method() !== 'PATCH') return r.continue();
    await heldP;
    await r.fulfill({ status: 500, json: { detail: 'disk full' } });
  });
  const sentP = page.waitForRequest(r => r.method() === 'PATCH' && /\/api\/devices\/em1$/.test(r.url()));
  await page.click('#devEditSave');
  await sentP;
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  releaseP();
  await page.waitForSelector('.toast.error', { timeout: 5000 }).catch(() => {});
  check('P a save that failed after its dialog closed is said (an error toast)',
        /disk full/.test(await page.locator('.toast.error').last().innerText().catch(() => '')));
  await page.unroute(/\/api\/devices\/em1$/);

  // ── Q. OPC UA "only the values I choose" with none ticked: refused here
  await go(page, 'em1');
  await menu(page, 'outputs');
  await waitOpen(page);
  await page.click('#outTab-opcua');
  if (!(await page.isChecked('#outEnabled'))) await page.check('#outEnabled');
  await page.check('input[name="ddvOpcuaMode"][value="chosen"]');
  await settled(page);
  check('Q none chosen: refused before Save, at the list, in the editor\'s words',
        (await page.getAttribute('#devEditSave', 'aria-disabled')) === 'true'
        && /Choose at least one value, or serve every value/.test(await page.locator('#devEditErrors').innerText())
        && (await page.getAttribute('#ddvOpcuaList', 'aria-invalid')) === 'true');
  await page.waitForSelector('#ddvOpcuaList input[type=checkbox]');
  await page.locator('#ddvOpcuaList input[type=checkbox]').first().check();
  await settled(page);
  check('Q …one ticked: the refusal goes', !(await page.locator('#devEditErrors li').count()));
  page.dlg.answers.push(true);
  await cancel(page);

  // ── R. a bus move, on the server's own field names: reviewed ─────────
  const reviewOf = async () => {
    const n = page.dlg.seen.length;
    page.dlg.answers.push(false);                     // Cancel: nothing saved
    await settled(page);
    await page.click('#devEditSave');
    await page.waitForTimeout(1200);
    return page.dlg.seen[n];
  };
  await go(page, 'rtu1');
  await menu(page, 'connection');
  await waitOpen(page);
  await page.locator('#ddvSerialAdv').evaluate(d => { d.open = true; }).catch(() => {});
  await page.fill('#ddvSerial', '/dev/ttyQA7');
  const rv1 = await reviewOf();
  check('R serial_port: reviewed, named', rv1 && /Review/.test(rv1.title) && rv1.details.some(x => /serial_port/.test(x)), rv1);
  page.dlg.answers.push(true);
  await cancel(page);
  await go(page, 'brpoll');
  await menu(page, 'connection');
  await waitOpen(page);
  // off the bridge: the reach "another address" (the connection step, L3.6)
  await page.click('label:has(#ddvReach-address)');
  await page.waitForSelector('#ddvHost');
  await page.fill('#ddvHost', '127.0.0.9');
  await page.fill('#ddvPort', '4005');
  const rv2 = await reviewOf();
  check('R bridge: reviewed, named', rv2 && rv2.details.some(x => /bridge/.test(x)), rv2);
  page.dlg.answers.push(true);
  await cancel(page);
  const mkR = await api.post('/api/devices', { data: { id: 'e2e-rtutcp', name: 'E2E converter', template: 'eastron_sdm120', enabled: false,
    connection: { protocol: 'rtu-tcp', host: '127.0.0.1', port: 9, unit_id: 6 } } });
  check('R (an RTU-over-TCP device)', mkR.ok(), await mkR.text());
  await go(page, 'e2e-rtutcp');
  await menu(page, 'connection');
  await waitOpen(page);
  await page.fill('#ddvHost', '127.0.0.8');
  const rv3 = await reviewOf();
  check('R host under rtu-tcp: reviewed, named', rv3 && rv3.details.some(x => /host/.test(x)), rv3);
  page.dlg.answers.push(true);
  await cancel(page);
  await api.delete('/api/devices/e2e-rtutcp');
  check('R nothing was saved', (await dev('rtu1')).connection.serial_port === before.rtu1.connection.serial_port);

  // ── S. a phone: picking a section keeps the focus on the list ────────
  const ph2 = await open('admin', { viewport: { width: 390, height: 844 }, hash: '#/devices/em1' });
  await ph2.waitForSelector('#ddvFacts .dev-sum-text');
  await ph2.waitForTimeout(500);
  await menu(ph2, 'edit');
  await waitOpen(ph2);
  await ph2.focus('#devEditRailSel');
  await ph2.selectOption('#devEditRailSel', 'outputs');
  check('S phone: the section shown, the focus still on the list',
        (await selected(ph2)) === 'outputs' && (await active(ph2)) === 'devEditRailSel');
  await ph2.close();

  // ── M. Upload map: the Model section alone (L3-F11) ──────────────────
  // the list read rtu1; then its connection is saved elsewhere; then a map
  // is uploaded and assigned — the connection saved meanwhile stays
  const exp = await (await api.get('/api/device-templates/eastron_sdm120')).json();
  exp.device_template.id = 'e2e_sdm120_copy';
  exp.device_template.name = 'E2E SDM120 copy';
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  await page.waitForSelector('#devicesListView', { state: 'visible' });
  const unitM = (await dev('rtu1')).connection.unit_id;
  const pm = await patch('rtu1', { connection: { protocol: 'rtu', unit_id: unitM === 9 ? 10 : 9 } });
  check('M the connection saved elsewhere', pm.ok(), await pm.text());
  const rM = page.waitForResponse(r => r.request().method() === 'PATCH' && /\/api\/devices\/rtu1$/.test(new URL(r.url()).pathname));
  await page.evaluate(d => window.app._uploadMapAndAssign(d, 'rtu1', true), exp);
  const respM = await rM;
  check('M assigned by PATCH {model} with the device\'s tag', respM.status() === 200 && !!respM.request().headers()['if-match']
        && JSON.stringify(respM.request().postDataJSON()) === '{"sections":{"model":{"template":"e2e_sdm120_copy"}}}', respM.request().postData());
  const rtuM = await dev('rtu1');
  check('M the template assigned, the connection saved meanwhile kept', rtuM.template === 'e2e_sdm120_copy'
        && rtuM.connection.unit_id === (unitM === 9 ? 10 : 9), { template: rtuM.template, unit: rtuM.connection.unit_id });
  await patch('rtu1', { model: { template: before.rtu1.template }, connection: { protocol: 'rtu', unit_id: unitM } });
  await api.delete('/api/device-templates/e2e_sdm120_copy');
} catch (e) {
  check('no exception', false, String(e && e.stack || e));
} finally {
  await restore().catch(() => {});
  for (const c of contexts) await c.close().catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
