// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// ADR-0030 in the browser: every write of the device page carries the tag
// the device was opened with.
//
//   A  two tabs on one device, each in the Edit modal (L3.4): tab B's change
//      is checked with the tag it opened with and refused (412); the modal
//      says so (role=alert), reads the version stored now, keeps what tab B
//      typed over it, marks the field both changed ("Stored now"), takes what
//      only tab A changed; the next Save goes through with both changes
//   B  one tab, through the page's controls: write lock, MQTT then InfluxDB
//      switched quickly (staged in the modal: one PATCH), publishing, a poll
//      interval, the type (Type…: the modal), Test — and calculated / meters
//      — each followed by a Save from the modal: never a false 412
//   C  the API: a stale tag on a sub-resource → 412; a runtime action never
//   D  an installation's unit renamed on its page (through its installation,
//      a route with no tag), then written: the page took the new tag
//   E  with CMD_URL (the `cmdui` fixture): command enable, binding, channel
//      pace, a component renamed — each followed by Save
//
// A throwaway gateway on the `qa` fixture (127.0.0.1 only, nothing real):
//
//   MBG_FX_NAME=etag-a tools/e2e/fixture_up.sh qa 18441
//   MBG_FX_NAME=etag-c tools/e2e/fixture_up.sh cmdui 18442
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18441 CMD_URL=http://127.0.0.1:18442 \
//     node tools/e2e/device_etag_e2e.mjs
//   docker rm -f etag-a etag-c
//
// It writes (names, unit ids, outputs): start the fixture fresh.
import { chromium } from 'playwright';
import { openDeviceTab, openDeviceEditor, deviceAction, editorModalOpen, editorSection, saveDeviceEditor, closeDeviceEditor } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18441';
const DEV = process.env.DEVICE || 'em2';
const ONLY = (process.env.ONLY || '').split(',').filter(Boolean);
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + String(extra).slice(0, 500) : ''}`);
};
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  return { status: r.status, body, headers: r.headers };
};
const device = async id => (await api(`/api/devices/${id}`)).body?.device || {};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(path = '/#/devices') {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  await ctx.addInitScript(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) { /* */ } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|status of (4\d\d|5\d\d)/.test(m.text())) page.errs.push(m.text()); });
  page.codes = [];
  page.on('response', r => {
    const q = r.request();
    if (q.method() !== 'GET') page.codes.push([q.method(), new URL(r.url()).pathname, r.status(), q.headers()['if-match'] || '']);
  });
  page.dlg = await watchDialogs(page);
  await page.goto(BASE + path);
  await page.waitForLoadState('load');
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 45000 });
  await page.evaluate(() => { window.app._firstRunShown = true; window.app.closeModal?.('firstRunModal'); });
  return page;
}
async function openDevice(page, id, tab) {
  await page.evaluate(id => window.app.openDeviceDetail(id), id);
  await page.waitForSelector('#deviceWsTabs');
  if (tab) await openDeviceTab(page, tab);
}
// Save: the Edit modal's (every kind, L3.11b) — answers what it said
// ("Saved", or the banner / the refusal); a full editor's (its edit bar)
async function save(page) {
  if (await editorModalOpen(page)) {
    const r = await saveDeviceEditor(page);
    if (r && r.ok()) return 'Saved';
    return (await page.locator('#devEditConflict, #devEditErrors').allInnerTexts()).join(' ').trim() || `HTTP ${r && r.status()}`;
  }
  return fullSave(page);
}
// a full-screen editor's one Save (Commands & bindings, Measurements, L3.11b)
async function fullSave(page) {
  await page.evaluate(() => { const f = document.getElementById('ddvEditFeedback'); if (f) f.textContent = ''; });
  await page.click('#ddvEditSave');
  await page.waitForFunction(() => /Saved|Salvat|Not saved|Nesalvat|Nothing|Nimic/.test(document.getElementById('ddvEditFeedback')?.textContent || ''));
  return page.locator('#ddvEditFeedback').innerText();
}
// the device's name, typed in the Edit modal (opened when it is not)
async function typeName(page, name) {
  if (!(await editorModalOpen(page))) await openDeviceTab(page, 'connection');
  await editorSection(page, 'identity');
  await page.fill('#ddvName', name);
}
const sent412 = (page, from = 0) => page.codes.slice(from).filter(([, , s]) => s === 412);

const SECTIONS = {};

// ── A. two tabs ─────────────────────────────────────────────────────────────
SECTIONS.A = async () => {
  const before = await device(DEV);
  const a = await open();
  const b = await open();
  // both tabs in the Edit modal (L3.4); tab A saves a name and a unit
  await openDevice(a, DEV, 'connection');
  await openDevice(b, DEV, 'connection');
  await a.fill('#ddvUnit', '4');
  await typeName(a, 'Garage meter (tab A)');
  await save(a);
  check('A1 tab A saves', (await device(DEV)).name === 'Garage meter (tab A)' && (await device(DEV)).connection?.unit_id === 4);
  // tab B types a unit: its dry run carries the tag B opened with — refused
  const n = b.codes.length;
  await b.fill('#ddvUnit', '5');
  await b.waitForSelector('#devEditConflict[role="alert"] .dev-shell-conflictbox');
  const patches = b.codes.slice(n).filter(([m, p]) => m === 'PATCH' && p === `/api/devices/${DEV}`);
  const now = await device(DEV);
  check('A2 tab B\'s change is checked with the tag it opened with and refused (412); nothing of B saved',
        patches.length >= 1 && patches[0][2] === 412 && patches[0][3] && now.name === 'Garage meter (tab A)'
        && now.connection?.unit_id === 4, `${JSON.stringify(patches)} now=${now.name}/${now.connection?.unit_id}`);
  const banner = b.locator('#devEditConflict[role="alert"] .dev-shell-conflictbox');
  check('A3 the modal says it (role=alert): changed elsewhere, edits kept, nothing saved',
        await banner.isVisible() && /changed elsewhere/i.test(await banner.innerText()) && /Nothing was saved/.test(await banner.innerText()));
  check('A4 B\'s typed unit is still on screen', (await b.inputValue('#ddvUnit')) === '5');
  check('A5 the field both changed is marked with what is stored now',
        await b.locator('#ddvUnit.ddv-kept').count() === 1 && /Stored now: 4/.test(await b.locator('#ddvUnit-kept').innerText()));
  await editorSection(b, 'identity');
  check('A6 what only tab A changed is taken: the name tab A stored', (await b.inputValue('#ddvName')) === 'Garage meter (tab A)',
        await b.inputValue('#ddvName'));
  const m = b.codes.length;
  await save(b);
  const after = await device(DEV);
  check('A7 the page shows no banner of its own (the modal said it)', !(await b.locator('#ddvConflict .ddv-conflict-box').count()));
  check('A8 the next Save goes through with both changes',
        !sent412(b, m).length && after.name === 'Garage meter (tab A)' && after.connection?.unit_id === 5,
        `${JSON.stringify(b.codes.slice(m))} ${after.name}/${after.connection?.unit_id}`);
  check('A9 no page errors', !a.errs.length && !b.errs.length, [...a.errs, ...b.errs].join(' | '));
  await a.context().close(); await b.context().close();
  await api(`/api/devices/${DEV}`, { method: 'PUT', body: JSON.stringify({ id: DEV, name: before.name, connection: { unit_id: before.connection?.unit_id } }) });
};

// ── B. the page's own writes, through its controls, never make Save a false 412
// Driven through the real controls; page.evaluate only for the calculated and
// meters editors (their modals have pickers that need live values the fixture
// does not read) — those two go through the same DeviceStore.devWrite.
SECTIONS.B = async () => {
  const before = await device(DEV);
  const page = await open();
  await openDevice(page, DEV, 'connection');
  const n0 = page.codes.length;
  let k = 0;
  // each step: something the page writes, then a rename saved from the Edit
  // modal (with whatever the step left staged in it: one PATCH)
  const step = async (label, fn) => {
    const n = page.codes.length;
    await fn();
    await page.waitForTimeout(300);
    await typeName(page, `Meter step ${++k}`);
    const fb = await save(page);
    const bad = sent412(page, n);
    check(`B${k} ${label}, then Save: no false 412`, !bad.length && (await device(DEV)).name === `Meter step ${k}`,
          `${JSON.stringify(page.codes.slice(n))} fb="${fb}"`);
  };
  const outputs = async (key) => {
    if (!(await editorModalOpen(page))) await openDeviceTab(page, 'outputs');
    else await editorSection(page, 'outputs');
    await page.click(`#outTab-${key}`);
    await page.waitForSelector(`#outTab-${key}[aria-selected="true"]`);
  };
  await closeDeviceEditor(page);
  await step('write lock switched (⋯ → Unlock writes… / Lock writes)', async () => {
    const answered = page.waitForResponse(r => /\/write-lock$/.test(new URL(r.url()).pathname));
    await deviceAction(page, '#ddvMenuLock');            // Unlock asks: the dialog answers yes
    await answered;
    await page.waitForFunction(() => document.activeElement?.id === 'ddvActions');   // the page drawn again
  });
  await step('MQTT then InfluxDB switched off quickly (staged: one PATCH with the rename)', async () => {
    await outputs('mqtt');
    // the two switches clicked in one go: MQTT's switch, the InfluxDB tab,
    // InfluxDB's switch — staged, nothing sent until Save
    const n = page.codes.length;
    await page.evaluate(() => {
      document.getElementById('outEnabled').click();
      document.getElementById('outTab-influxdb').click();
      document.getElementById('outEnabled').click();
    });
    await page.waitForTimeout(600);
    check('B: the switches are staged (nothing written yet)', !page.codes.slice(n).some(([, p]) => p === `/api/devices/${DEV}/outputs`)
          && (await device(DEV)).mqtt_enabled !== false);
  });
  {
    const d = await device(DEV);
    check('B: both switches went out with the Save (no banner)', d.mqtt_enabled === false && d.influxdb_enabled === false
          && !(await page.locator('#ddvConflict .ddv-conflict-box').count()), JSON.stringify({ m: d.mqtt_enabled, i: d.influxdb_enabled }));
  }
  await step('publishing mode staged', async () => {
    await outputs('mqtt');
    await page.selectOption('#pubMode-mqtt', 'all');
  });
  await step('a poll interval typed (the same PATCH)', async () => {
    if (!(await editorModalOpen(page))) await openDeviceTab(page, 'connection');
    await editorSection(page, 'polling');
    const inp = page.locator('#devEditPanel-polling .ddv-pg').first();
    if (await inp.count()) await inp.fill(String(Number(await inp.inputValue()) + 1));
  });
  await step('its type, from the ⋯ menu (Type…: the modal at Identity)', async () => {
    await deviceAction(page, '#ddvSetKind');
    await page.waitForSelector('#devEditModal.active #devEditType');
    const opts = await page.$$eval('#devEditType option', os => os.map(o => [o.value, o.selected]));
    const pick = opts.find(([v, sel]) => v && !sel);
    await page.selectOption('#devEditType', pick[0]);
  });
  await step('Test, from the head (a runtime action)', async () => {
    await page.click('#ddvHeadTest');
    await page.waitForFunction(() => !document.getElementById('ddvHeadTest')?.disabled);
  });
  await step('a calculated value (page.evaluate: the editor needs live values)', () => page.evaluate(id =>
    window.DeviceStore.devWrite(id, 'POST', `/api/devices/${id}/calculated`, { calculated: [{ name: 'twice', expr: '2 * 2' }] }), DEV));
  await step('meters (page.evaluate: the editor needs a meter that reads)', () => page.evaluate(id =>
    window.DeviceStore.devWrite(id, 'PUT', `/api/devices/${id}/meters`, { meters: [] }), DEV));
  const writes = page.codes.slice(n0).filter(([, p]) => p.startsWith(`/api/devices/${DEV}`));
  check('B: every write of the page sent If-Match', writes.length > 0 && writes.every(([, , , im]) => im), JSON.stringify(writes.filter(w => !w[3])));
  check('B: no banner on the page', !(await page.locator('#ddvConflict .ddv-conflict-box').count()));
  check('B: no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
  // leave it as it was
  await api(`/api/devices/${DEV}`, { method: 'PUT', body: JSON.stringify({ id: DEV, name: before.name }) });
  await api(`/api/devices/${DEV}/outputs`, { method: 'POST', body: JSON.stringify({ mqtt: before.mqtt_enabled !== false, influxdb: before.influxdb_enabled !== false }) });
  await api(`/api/devices/${DEV}/publishing`, { method: 'POST', body: JSON.stringify({ mqtt: { publish_mode: null } }) });
  await api(`/api/devices/${DEV}/calculated`, { method: 'POST', body: JSON.stringify({ calculated: [] }) });
  await api(`/api/devices/${DEV}/organise`, { method: 'PATCH', body: JSON.stringify({ type: null }) });
  await api(`/api/devices/${DEV}/write-lock`, { method: 'POST', body: JSON.stringify({ locked: !!before.write_locked }) });
};

// ── D. an installation's unit: renamed through its installation (a route with
// no tag), then written on its page — the page took the unit's new tag
SECTIONS.D = async () => {
  const UNIT = process.env.UNIT || 'sunfield-meter-240';
  const before = await device(UNIT);
  if (!before.id) { console.log(`SKIP D: no unit ${UNIT}`); return; }
  const page = await open();
  await openDevice(page, UNIT, 'connection');           // a unit: Rename…, the Edit modal at Identity (L3.11b)
  const n = page.codes.length;
  await page.fill('#ddvName', 'Grid meter (renamed)');
  await saveDeviceEditor(page);
  await page.waitForFunction(() => /Grid meter \(renamed\)/.test(document.querySelector('#deviceDetailView h2')?.textContent || ''));
  check('D1 the unit renamed in the modal (by its unit route)', (await device(UNIT)).name === 'Grid meter (renamed)');
  // the unit's lock (its installation's), from its ⋯
  const answered = page.waitForResponse(r => /\/write-lock$/.test(new URL(r.url()).pathname));
  await deviceAction(page, '#ddvMenuLock');
  await answered;
  await page.waitForFunction(() => document.activeElement?.id === 'ddvActions');
  const lockW = page.codes.slice(n).filter(([, p]) => p === `/api/devices/${UNIT}/write-lock`);
  check('D2 the next write of the page carries the new tag: no false 412',
        lockW.length === 1 && lockW[0][2] === 200 && lockW[0][3] && !sent412(page, n).length
        && !(await page.locator('#ddvConflict .ddv-conflict-box').count()), JSON.stringify(page.codes.slice(n)));
  check('D3 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
  await api(`/api/devices/${UNIT}/write-lock`, { method: 'POST', body: JSON.stringify({ locked: !!before.write_locked }) });
};

// ── E. commands and components (a gateway with a GX of its own: CMD_URL, the
// `cmdui` fixture) — command enable, binding, channel, a component renamed
SECTIONS.E = async () => {
  const URL_ = process.env.CMD_URL;
  if (!URL_) { console.log('SKIP E: no CMD_URL (the cmdui fixture)'); return; }
  const GX = process.env.CMD_DEVICE || 'gx';
  const capi = async (p, opts = {}) => {
    const r = await fetch(URL_ + p, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
    let body = null; try { body = await r.json(); } catch { /* */ }
    return { status: r.status, body };
  };
  await capi(`/api/devices/${GX}/components`, { method: 'POST', body: JSON.stringify({ type: 'temperature', instance: '23', name: 'Outside' }) });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  await ctx.addInitScript(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) { /* */ } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = []; page.codes = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('response', r => { const q = r.request(); if (q.method() !== 'GET') page.codes.push([q.method(), new URL(r.url()).pathname, r.status(), q.headers()['if-match'] || '']); });
  page.dlg = await watchDialogs(page);
  await page.goto(URL_ + '/#/devices');
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 45000 });
  await page.evaluate(() => { window.app._firstRunShown = true; window.app.closeModal?.('firstRunModal'); });
  // a command's Enabled tick, its binding and its channel edit only in the
  // commands editor (⋯ → Commands & bindings…, full screen, staged: its
  // one Save writes them — L3.11b)
  await openDevice(page, GX);
  await openDeviceEditor(page, 'commands');
  let k = 0;
  const step = async (label, fn) => {
    const n = page.codes.length;
    await fn();
    await page.waitForTimeout(400);
    await typeName(page, `GX step ${++k}`);
    const fb = await save(page);
    const now = (await capi(`/api/devices/${GX}`)).body?.device || {};
    check(`E${k} ${label}, then Save: no false 412`, !sent412(page, n).length && now.name === `GX step ${k}`,
          `${JSON.stringify(page.codes.slice(n))} fb="${fb}"`);
    await openDeviceEditor(page, 'commands');
  };
  await step('a command enabled / disabled', async () => {
    const cb = page.locator('.cmd-card[data-cmd="max_charge_power"] input[type="checkbox"][data-action="toggleDeviceCommand"]');
    await cb.click();
    await fullSave(page);
  });
  await step('a binding saved (lease + safe value)', async () => {
    const ess = page.locator('.cb-bind[data-cb-name="ess_setpoint"]');
    const lease = String((Number(await ess.locator('#dcb_b_ess_setpoint_lease').inputValue()) || 60) + 5);
    await ess.locator('#dcb_b_ess_setpoint_lease').fill(lease);
    await ess.locator('#dcb_b_ess_setpoint_safe_value').fill('0');
    await fullSave(page);
    await page.waitForFunction(v => document.querySelector('#dcb_b_ess_setpoint_lease')?.defaultValue === v, lease);
  });
  await step('a channel pace saved', async () => {
    const ch = page.locator('.cb-chan[data-cb-cid="ess_setpoint"]');
    const pace = String((Number(await ch.locator('#dcb_c_ess_setpoint_min_interval_s').inputValue()) || 25) + 5);
    await ch.locator('#dcb_c_ess_setpoint_min_interval_s').fill(pace);
    await fullSave(page);
    await page.waitForFunction(v => document.querySelector('#dcb_c_ess_setpoint_min_interval_s')?.defaultValue === v, pace);
  });
  await step('a component renamed', async () => {
    await openDeviceEditor(page, 'components');      // the components card, editing (⋯ → Components…)
    const btn = page.locator(`#devComponentsCard tr[data-comp="${GX}-temperature-23"] [data-action="renameComponentUi"]`);
    await btn.waitFor();
    page.dlg.answers.push('Garden');
    await btn.click();
    await page.waitForFunction(id => /Garden/.test(document.querySelector(`#devComponentsCard tr[data-comp="${id}"]`)?.textContent || ''), `${GX}-temperature-23`);
  });
  check('E: no banner, no page errors', !(await page.locator('#ddvConflict .ddv-conflict-box').count()) && !page.errs.length, page.errs.join(' | '));
  await ctx.close();
  await capi(`/api/devices/${GX}/components/${GX}-temperature-23`, { method: 'DELETE' });
};

// ── C. the API itself, as the page meets it ────────────────────────────────
SECTIONS.C = async () => {
  const r = await fetch(`${BASE}/api/devices/${DEV}`);
  const tag = r.headers.get('etag');
  check('C1 GET one device carries an ETag', !!tag);
  const stale = await api(`/api/devices/${DEV}/outputs`, { method: 'POST', headers: { 'If-Match': '"stale"' }, body: JSON.stringify({ mqtt: true }) });
  check('C2 a sub-resource write with a stale tag: 412 with the current entry and tag',
        stale.status === 412 && stale.body?.detail?.etag === tag && stale.headers.get('etag') === tag);
  const t = await api(`/api/devices/${DEV}/test`, { method: 'POST', headers: { 'If-Match': '"stale"' } });
  check('C3 a runtime action never answers 412, and says the tag', t.status !== 412 && t.headers.get('etag') === tag
        && t.headers.get('mbg-if-match') === 'ignored', `${t.status}`);
};

try {
  for (const [k, fn] of Object.entries(SECTIONS)) {
    if (ONLY.length && !ONLY.includes(k)) continue;
    console.log(`\n── ${k} ──`);
    try { await fn(); } catch (e) { check(`${k} ran to the end`, false, e && e.stack || e); }
  }
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
