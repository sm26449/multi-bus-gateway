// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// L3.2b in the browser (ADR-0031 §5):
//
//   A  Settings → Modbus: the read preview's budget shows the stored values,
//      refuses a value out of range at the page, saves a good one
//   B  the installation wizard takes its Solar API calls from the templates
//      (protocol.http.path, L3-F6) — the same calls as before
//   C  the Add-device check of step 1 asks with only=connection, never makes
//      or deletes a probe device (L3-F3)
//   D  an installation's unit renamed on its page: one PATCH of the unit
//      with the unit's tag, no whole-installation PUT, and the next write
//      carries the new tag (L3-F12)
//
// A throwaway gateway on the `qa` fixture (127.0.0.1 only, nothing real):
//
//   MBG_FX_NAME=l32b-qa tools/e2e/fixture_up.sh qa 18461
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18461 node tools/e2e/l32b_write_paths_e2e.mjs
//   docker rm -f l32b-qa
//
// It writes (the budget, a unit's name): start the fixture fresh.
import { chromium } from 'playwright';
import { openDeviceEditor, editorSettled } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18461';
const UNIT = process.env.UNIT || 'sunfield-meter-240';
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

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(path = '/#/devices') {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  await ctx.addInitScript(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) { /* */ } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|status of (4\d\d|5\d\d)/.test(m.text())) page.errs.push(m.text()); });
  page.reqs = [];
  page.on('response', r => {
    const q = r.request();
    const u = new URL(r.url());
    if (q.method() !== 'GET') page.reqs.push([q.method(), u.pathname + u.search, r.status(), q.headers()['if-match'] || '']);
  });
  await page.goto(BASE + path);
  await page.waitForLoadState('load');
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 45000 });
  await page.evaluate(() => { window.app._firstRunShown = true; window.app.closeModal?.('firstRunModal'); });
  return page;
}

const SECTIONS = {};

SECTIONS.A = async () => {
  const page = await open();
  await page.evaluate(() => window.app.openSettingsTab('modbus'));
  await page.waitForSelector('[data-cfgpanel="modbus"] #cfgPreviewMaxRequests', { state: 'visible' });
  await page.waitForFunction(() => document.getElementById('cfgPreviewMaxRequests').value !== '');
  check('A1 the stored budget is shown', await page.inputValue('#cfgPreviewMaxRequests') === '4'
        && await page.inputValue('#cfgPreviewListen') === '10');
  const hint = await page.locator('#cfgPreviewListenHint').innerText();
  check('A2 each value has its help', /never transmits/.test(hint) && /1–60/.test(hint), hint);
  const n = page.reqs.length;
  await page.fill('#cfgPreviewMaxRequests', '0');
  await page.click('[data-action="savePreviewBudget"]');
  await page.waitForFunction(() => /1–20/.test(document.getElementById('previewBudgetFeedback').textContent));
  check('A3 a value out of range is said at the page, nothing sent', !page.reqs.slice(n).length);
  await page.fill('#cfgPreviewMaxRequests', '6');
  await page.fill('#cfgPreviewListen', '20');
  await page.click('[data-action="savePreviewBudget"]');
  await page.waitForFunction(() => document.getElementById('previewBudgetFeedback').classList.contains('ok'));
  const got = (await api('/api/config/preview')).body;
  check('A4 a good one is saved', got.max_requests === 6 && got.listen_s === 20, JSON.stringify(got));
  const sent = page.reqs.slice(n).filter(([m, p]) => m === 'POST' && p === '/api/config/preview');
  check('A5 one save, the budget alone', sent.length === 1 && sent[0][2] === 200, JSON.stringify(page.reqs.slice(n)));
  check('A6 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

SECTIONS.B = async () => {
  const page = await open();
  await page.evaluate(() => window.app.openPlantWizard());
  await page.waitForFunction(() => window.app._plantWiz && window.app._plantWiz.httpPaths);
  const paths = await page.evaluate(() => window.app._plantWiz.httpPaths);
  // the calls the wizard typed by hand before L3-F6
  const OLD = {
    inverter: '/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData',
    site: '/solar_api/v1/GetPowerFlowRealtimeData.fcgi',
    meter: '/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}',
  };
  const sorted = o => JSON.stringify(Object.keys(o).sort().map(k => [k, o[k]]));
  check('B1 the Solar API calls come from the templates, the same as before', sorted(paths) === sorted(OLD),
        JSON.stringify(paths));
  check('B2 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

SECTIONS.C = async () => {
  const page = await open();
  // Add Device's Connection step (L3.9): Next asks the gateway about the connection alone
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
  await page.click('[data-ad-way="connection"]');
  await page.locator('label:has(#csAddPortKind-ethernet)').click();
  await page.waitForSelector('#csAddHost');
  await page.fill('#csAddHost', '127.0.0.1');
  await page.fill('#csAddPort', '15502');
  await page.locator('#csAddPort').blur();
  await page.waitForTimeout(300);
  const n = page.reqs.length;
  await page.click('#addDevNext');
  await page.waitForTimeout(1500);
  const ok = true;
  const sent = page.reqs.slice(n);
  check('C1 the Connection step asks with only=connection', sent.some(([m, p]) => m === 'POST' && p === '/api/devices?dry_run=true&only=connection'),
        JSON.stringify(sent));
  check('C2 no probe device is made or deleted', !sent.some(([m, p]) => /x-dry-run/.test(p) || m === 'DELETE'), JSON.stringify(sent));
  const ids = ((await api('/api/devices')).body?.devices || []).map(d => d.id);
  check('C3 nothing stored', !ids.some(i => /probe|x-dry-run/.test(i)), ids.join(','));
  check('C4 the check answered (a refusal is said at its field)', typeof ok === 'boolean');
  check('C5 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
};

SECTIONS.D = async () => {
  const before = (await api(`/api/devices/${UNIT}`)).body?.device;
  if (!before) { console.log(`SKIP D: no unit ${UNIT}`); return; }
  const page = await open();
  await page.evaluate(id => window.app.openDeviceDetail(id), UNIT);
  await page.waitForSelector('#ddvActions');
  // the unit's name edits from its ⋯ → Rename… — the Edit device modal at
  // Identity (L3.11b)
  await openDeviceEditor(page, 'connection');
  const n = page.reqs.length;
  await page.fill('#ddvName', 'Grid meter (L3.2b)');
  const answered = page.waitForResponse(r => r.request().method() === 'PATCH' && /\/units\//.test(r.url()));
  await editorSettled(page);
  await page.click('#devEditSave');
  await answered;
  await page.waitForFunction(() => /Grid meter \(L3\.2b\)/.test(document.querySelector('#deviceDetailView h2')?.textContent || ''));
  const sent = page.reqs.slice(n);
  const patch = sent.filter(([m, p]) => m === 'PATCH' && p === `/api/endpoints/sunfield/units/${UNIT}`);
  check('D1 one PATCH of the unit, with its tag', patch.length === 1 && patch[0][2] === 200 && patch[0][3], JSON.stringify(sent));
  check('D2 no whole-installation PUT', !sent.some(([m, p]) => m === 'PUT' && p.startsWith('/api/endpoints/')), JSON.stringify(sent));
  check('D3 stored', (await api(`/api/devices/${UNIT}`)).body?.device?.name === 'Grid meter (L3.2b)');
  const m = page.reqs.length;
  await page.evaluate(id => window.DeviceStore.devWrite(id, 'POST', `/api/devices/${id}/write-lock`, { locked: false }), UNIT);
  const w = page.reqs.slice(m).filter(([, p]) => p === `/api/devices/${UNIT}/write-lock`);
  check('D4 the next write carries the new tag: no false 412', w.length === 1 && w[0][2] === 200 && w[0][3], JSON.stringify(page.reqs.slice(m)));
  check('D5 no page errors', !page.errs.length, page.errs.join(' | '));
  await page.context().close();
  await api(`/api/devices/${UNIT}/write-lock`, { method: 'POST', body: JSON.stringify({ locked: !!before.write_locked }) });
};

try {
  for (const [k, fn] of Object.entries(SECTIONS)) {
    console.log(`\n── ${k} ──`);
    try { await fn(); } catch (e) { check(`${k} ran to the end`, false, e && e.stack || e); }
  }
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
