// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Delete and Restore (ADR-0032 §8, L3.11c), as a user meets them:
// A  Delete… from the device page: the dialog, then the list with a toast
//    that offers Undo for 10 s; the Undo button has the focus (the item that
//    had it is gone); the toast says plainly what Undo cannot bring back (an
//    unlocked device comes back write-locked); Enter restores it, the page
//    it was deleted from opens again, and the toast says what was reset
// B  Delete… from a list row: the toast ends by itself after 10 s and the
//    focus goes to the list's heading, never to the page's body; the device
//    is then in "Deleted devices"
// C  Esc dismisses the toast (focus to the heading)
// D  ⋯ → "Deleted devices: restore…" on a list row opens the Restore dialog:
//    every deleted device, one that cannot come back as it was says why
//    before the click (its Restore held, the reason read with it), one
//    restored from there is back; Esc closes it and the focus goes back to
//    the ⋯ it was opened from
// E  the same line on a device page opens the dialog over the page
// F  Romanian: the toast and the dialog in Romanian
// G  a phone (390 px): the dialog fits, nothing scrolls sideways
// H  the used-by flow is as it was: a used device's Delete is held, no toast
// The review (2026-11-01):
// I  m4: Undo after the user went elsewhere does not pull them back — the
//    restored toast offers "Open it"; m1: the restore names the delete
// J  m2: a device already back (restored meanwhile) is said as back, not failed
// K  m6: a REST push that starts again with it is said, with its host
// L  M2: a malformed kept device is listed with why, and can be forgotten
//    (FX_CFG: the fixture's config dir on this host, where it is planted)
//
// Against a throwaway gateway of the qa fixture, login on (127.0.0.1 only):
//   MBG_FX_NAME=mbg-fx-l311c tools/e2e/fixture_up.sh qa 18411 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18411 MBG_PASS=e2e-pass FX_CFG=$TMPDIR/mbg-fx-l311c node tools/e2e/undo_restore_e2e.mjs
//   docker rm -f mbg-fx-l311c
// It adds ur-* devices and removes them (and their kept settings) at the end.
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { deviceAction } from './lib/device-page.mjs';
import { mkdirSync, writeFileSync, chmodSync } from 'fs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18411';
const PASS = process.env.MBG_PASS || 'e2e-pass';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : ''}`);
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

const api = await pwRequest.newContext({ baseURL: BASE });
const login = await api.post('/api/auth/login', { data: { username: 'admin', password: PASS } });
const state = await api.storageState();
const get = async u => (await api.get(u)).json().catch(() => ({}));
const post = async (u, data) => { const r = await api.post(u, { data }); return { status: r.status(), body: await r.json().catch(() => ({})) }; };
const exists = async id => (await api.get(`/api/devices/${id}`)).status() === 200;
const IDS = ['ur-a', 'ur-b', 'ur-c', 'ur-p', 'ur-t', 'ur-n', 'ur-r', 'ur-bad', 'ur-bad-conn', 'ur-bad-list', 'ur-bad-top'];
async function cleanup() {
  for (const id of IDS) await api.delete(`/api/devices/${id}`).catch(() => {});
  for (const id of IDS) await api.delete(`/api/devices/restorable/${id}`).catch(() => {});
}
const tcp = (id, port) => ({ id, name: `UR ${id.slice(3).toUpperCase()}`, template: 'eastron_sdm120', enabled: false,
  connection: { protocol: 'tcp', host: '127.0.0.1', port, unit_id: 1 } });
async function setup() {
  await cleanup();
  for (const d of [tcp('ur-a', 15701), tcp('ur-b', 15702), tcp('ur-c', 15703)]) {
    const r = await post('/api/devices', d);
    if (r.status !== 200) throw new Error(`create ${d.id}: ${JSON.stringify(r.body)}`);
  }
  // ur-a unlocked: Undo cannot bring that back, and must say so
  const u = await post('/api/devices/ur-a/write-lock', { locked: false });
  if (u.status !== 200) throw new Error(`unlock ur-a: ${JSON.stringify(u.body)}`);
  // ur-p polled /dev/ttyQA5; deleted, then a listener takes the bus: it
  // cannot come back as it was
  let r = await post('/api/devices', { id: 'ur-p', name: 'UR poller', template: 'eastron_sdm120', enabled: false,
    connection: { protocol: 'rtu', serial_port: '/dev/ttyQA5', unit_id: 5, baudrate: 9600 } });
  if (r.status !== 200) throw new Error(`create ur-p: ${JSON.stringify(r.body)}`);
  await api.delete('/api/devices/ur-p');
  r = await post('/api/devices', { id: 'ur-t', name: 'UR listener', template: 'eastron_sdm120', enabled: false,
    connection: { protocol: 'rtu_tap', serial_port: '/dev/ttyQA5', unit_id: 5, baudrate: 9600 } });
  if (r.status !== 200) throw new Error(`create ur-t: ${JSON.stringify(r.body)}`);
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|409|412|422/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  if (opts.lang) await page.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, opts.lang);
  await page.goto(BASE + (opts.hash || '/#/devices'));
  await page.waitForLoadState('load');
  await page.waitForSelector('#devicesList .device-row', { timeout: 15000 }).catch(() => {});
  return page;
}
const go = async (page, hash) => {
  await page.evaluate(h => { location.hash = h; }, hash);
  await page.waitForSelector('#deviceDetailView #ddvFacts', { state: 'visible' }).catch(() => {});
  await page.waitForTimeout(600);
};
const toast = page => page.locator('#toast-container .undo-toast');
const focusIs = (page, sel) => page.evaluate(s => !!document.activeElement && document.activeElement.matches(s), sel);
const onBody = page => page.evaluate(() => !document.activeElement || document.activeElement === document.body);
async function rowMenu(page, id) {
  await page.waitForSelector(`#devicesList .device-row[data-f="${id}"] [data-dev-menu]`);
  await page.click(`#devicesList .device-row[data-f="${id}"] [data-dev-menu]`);
  const menu = `#devMenu-list-${id}`;
  await page.waitForSelector(`${menu}:not([hidden])`);
  await page.waitForFunction(sel => !document.querySelector(`${sel} .is-checking`), menu).catch(() => {});
  return menu;
}

try {
  check('logged in (the fixture\'s login)', login.ok());
  await setup();

  // ── A · from the device page: Undo, by keyboard ────────────────────────
  let page = await open();
  await go(page, '#/devices/ur-a');
  page.dlg.answer = true;
  await deviceAction(page, '[data-ddv-act="delete"]');
  await page.waitForSelector('#toast-container .undo-toast', { timeout: 10000 });
  check('A: deleted', !(await exists('ur-a')));
  check('A: back on the list', await page.locator('#devicesListView').isVisible());
  const tA = await toast(page).innerText();
  check('A: the toast names the device', /UR A/.test(tA) && /ur-a/.test(tA), tA);
  check('A: it says plainly what Undo cannot bring back (it comes back write-locked)', /write-locked/i.test(tA), tA);
  check('A: Undo has the focus (the item that had it is gone)', await focusIs(page, '#toast-container .undo-toast .toast-undo'));
  check('A: the toast sits in the polite live region', await page.evaluate(() =>
    document.querySelector('.undo-toast')?.closest('[aria-live="polite"]') !== null));
  check('A: the seconds left are shown', /\b(10|9|8)\b/.test(await toast(page).locator('.toast-undo').innerText()));
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => !document.querySelector('#toast-container .undo-toast'), null, { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(800);
  check('A: Undo restored it', await exists('ur-a'));
  check('A: its page opens again (it was deleted from there)', await page.evaluate(() => window.app._devDetail?.id === 'ur-a')
        && await page.locator('#deviceDetailView').isVisible());
  const said = await page.locator('#toast-container .toast').allInnerTexts();
  check('A: the restore says what it reset', said.some(s => /restored/i.test(s) && /write-locked/i.test(s)), said);
  const devA = (await get('/api/devices/ur-a')).device || {};
  check('A: its name and connection as they were', devA.name === 'UR A' && +devA.port === 15701, devA);
  check('A: no page errors', page.errs.length === 0, page.errs);

  // ── B · from a list row: the toast ends by itself after 10 s ──────────
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  let menu = await rowMenu(page, 'ur-b');
  await page.click(`${menu} [data-ddv-act="delete"]`);
  await page.waitForSelector('#toast-container .undo-toast');
  const tB = await toast(page).innerText();
  check('B: an already locked device: no reset said', !/write-locked/i.test(tB), tB);
  check('B: Undo has the focus', await focusIs(page, '#toast-container .undo-toast .toast-undo'));
  await sleep(9000);
  check('B: still there at 9 s', await toast(page).count() === 1);
  await sleep(2500);
  check('B: gone after 10 s', await toast(page).count() === 0);
  check('B: the focus went to the list\'s heading, not to the page\'s body',
        !(await onBody(page)) && await focusIs(page, '#devicesListView h2'));
  check('B: not restored', !(await exists('ur-b')));
  const rest = (await get('/api/devices/restorable')).devices.map(d => d.id);
  check('B: it is in "Deleted devices"', rest.includes('ur-b'), rest);

  // ── C · Esc dismisses ───────────────────────────────────────────────────
  menu = await rowMenu(page, 'ur-c');
  await page.click(`${menu} [data-ddv-act="delete"]`);
  await page.waitForSelector('#toast-container .undo-toast');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  check('C: Esc dismissed the toast', await toast(page).count() === 0);
  check('C: focus on the heading', await focusIs(page, '#devicesListView h2'));
  check('C: not restored', !(await exists('ur-c')));

  // ── D · the Restore dialog from a list row ──────────────────────────────
  menu = await rowMenu(page, 'em2');
  await page.click(`${menu} [data-ddv-act="restore"]`);
  await page.waitForSelector('#restoreModal.active');
  await page.waitForSelector('#restoreList [data-restore-row="ur-b"]');
  check('D: the dialog, not a scroll to the folded section', await page.locator('#restoreModal.active').count() === 1
        && await page.evaluate(() => window.scrollY) < 200);
  const rows = await page.locator('#restoreList [data-restore-row]').evaluateAll(els => els.map(e => e.dataset.restoreRow));
  check('D: every deleted device listed', ['ur-b', 'ur-c', 'ur-p'].every(x => rows.includes(x)), rows);
  const pRow = page.locator('#restoreList [data-restore-row="ur-p"]');
  const pText = await pRow.innerText();
  check('D: one that cannot come back says why, before the click (the listener named)', /ur-t/.test(pText), pText);
  const pBtn = pRow.locator('[data-restore-id="ur-p"]');
  check('D: its Restore held, the reason read with it', (await pBtn.getAttribute('aria-disabled')) === 'true'
        && /ur-t/.test(await page.evaluate(() => {
          const b = document.querySelector('[data-restore-id="ur-p"]');
          return (b.getAttribute('aria-describedby') || '').split(' ').map(i => document.getElementById(i)?.textContent || '').join(' ');
        })));
  await pBtn.click({ force: true });
  await page.waitForTimeout(400);
  check('D: a held Restore restores nothing', !(await exists('ur-p')));
  const bRow = page.locator('#restoreList [data-restore-row="ur-b"]');
  check('D: one that can: no problem said, write-locked said only where it changes', !/write-locked/i.test(await bRow.innerText()), await bRow.innerText());
  await bRow.locator('[data-restore-id="ur-b"]').click();
  await page.waitForFunction(() => /restored/i.test(document.querySelector('#restoreList [data-restore-row="ur-b"]')?.textContent || ''), null, { timeout: 10000 }).catch(() => {});
  check('D: restored from the dialog', await exists('ur-b'));
  check('D: the row says so, with the way to it', /restored/i.test(await bRow.innerText())
        && await bRow.locator('[data-restore-open="ur-b"]').count() === 1, await bRow.innerText());
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  check('D: Esc closes it', await page.locator('#restoreModal.active').count() === 0);
  check('D: the focus back on the ⋯ it was opened from', await page.evaluate(() => document.activeElement?.dataset.devMenu === 'em2'));
  check('D: the list shows the restored device', await page.locator('#devicesList .device-row[data-f="ur-b"]').count() === 1);

  // ── E · from a device page ──────────────────────────────────────────────
  await go(page, '#/devices/em1');
  await deviceAction(page, '[data-ddv-act="restore"]');
  await page.waitForSelector('#restoreModal.active');
  check('E: the dialog over the page (still on em1)', await page.evaluate(() => window.app._devDetail?.id === 'em1')
        && await page.locator('#deviceDetailView').isVisible());
  await page.keyboard.press('Escape');
  check('D–E: no page errors', page.errs.length === 0, page.errs);
  await page.context().close();

  // ── F · Romanian ────────────────────────────────────────────────────────
  page = await open({ lang: 'ro' });
  menu = await rowMenu(page, 'ur-b');
  await page.click(`${menu} [data-ddv-act="delete"]`);
  await page.waitForSelector('#toast-container .undo-toast');
  const tF = await toast(page).innerText();
  check('F: the toast in Romanian (Anulează)', /Anulează/.test(tF), tF);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(800);
  check('F: Undo restored it', await exists('ur-b'));
  menu = await rowMenu(page, 'em2');
  await page.click(`${menu} [data-ddv-act="restore"]`);
  await page.waitForSelector('#restoreModal.active');
  const title = await page.locator('#restoreModalTitle').innerText();
  check('F: the dialog in Romanian', /șterse/i.test(title), title);
  const pRo = await page.locator('#restoreList [data-restore-row="ur-p"]').innerText();
  check('F: the conflict in the page\'s words around the gateway\'s', /Nu poate fi restaurat/.test(pRo), pRo);
  await page.keyboard.press('Escape');
  await page.context().close();

  // ── G · a phone ─────────────────────────────────────────────────────────
  page = await open({ viewport: { width: 390, height: 844 } });
  await page.evaluate(() => window.app.openRestoreDialog());
  await page.waitForSelector('#restoreModal.active');
  await page.waitForTimeout(400);
  const box = await page.locator('#restoreModal .modal-content').boundingBox();
  check('G: the dialog fits the phone', box && box.x >= 0 && box.x + box.width <= 390, box);
  check('G: nothing scrolls sideways', await page.evaluate(() => document.documentElement.scrollWidth <= 390));
  await page.keyboard.press('Escape');
  await page.context().close();

  // ── H · the used-by flow as it was ─────────────────────────────────────
  page = await open();
  await go(page, '#/devices/em1');                 // ups1 is measured by em1
  await deviceAction(page, '[data-ddv-act="delete"]');
  await page.waitForTimeout(1200);
  check('H: a used device is not deleted, no Undo toast', await exists('em1') && await toast(page).count() === 0);
  check('H: no page errors', page.errs.length === 0, page.errs);
  await page.context().close();

  // ── I · Undo after going elsewhere (m4); the restore names its delete (m1) ──
  {
    const r = await post('/api/devices', tcp('ur-n', 15711));
    if (r.status !== 200) throw new Error(`create ur-n: ${JSON.stringify(r.body)}`);
  }
  page = await open();
  const restores = [];
  page.on('request', q => { if (/\/restore(\?|$)/.test(q.url())) restores.push(q.url()); });
  await go(page, '#/devices/ur-n');
  await deviceAction(page, '[data-ddv-act="delete"]');
  await page.waitForSelector('#toast-container .undo-toast');
  await page.evaluate(() => { location.hash = '#/rules'; });
  await page.waitForTimeout(1200);
  await page.click('#toast-container .undo-toast .toast-undo');
  await page.waitForTimeout(1500);
  check('I: restored', await exists('ur-n'));
  check('I: the user stays where they went (Rules), not pulled back to the device',
        /^#\/rules/.test(await page.evaluate(() => location.hash)) && await page.evaluate(() => window.app._view?.type !== 'device'),
        await page.evaluate(() => location.hash));
  const openIt = page.locator('#toast-container .toast [data-toast-act]');
  check('I: the restored toast offers "Open it"', await openIt.count() === 1 && /Open it/.test(await openIt.innerText()));
  check('I: the restore named the delete it undoes (deleted_ts)', restores.some(u => /deleted_ts=\d/.test(u)), restores);
  await openIt.click();
  await page.waitForTimeout(800);
  check('I: Open it opens the device', await page.evaluate(() => window.app._devDetail?.id === 'ur-n'));

  // ── J · already back (m2) ───────────────────────────────────────────────
  await page.evaluate(() => { location.hash = '#/devices'; });
  await page.waitForTimeout(800);
  menu = await rowMenu(page, 'ur-n');
  await page.click(`${menu} [data-ddv-act="delete"]`);
  await page.waitForSelector('#toast-container .undo-toast');
  const rb = await post('/api/devices/ur-n/restore', {});
  check('J: restored meanwhile (another tab)', rb.status === 200, rb);
  await page.click('#toast-container .undo-toast .toast-undo');
  await page.waitForTimeout(1000);
  const saidJ = await page.locator('#toast-container .toast').allInnerTexts();
  check('J: said as already back, not as a failure', saidJ.some(x => /already back/i.test(x)) && !saidJ.some(x => /Undo failed/i.test(x)), saidJ);

  // ── K · its REST push starts again, said (m6) ──────────────────────────
  {
    const r = await post('/api/devices', tcp('ur-r', 15712));
    if (r.status !== 200) throw new Error(`create ur-r: ${JSON.stringify(r.body)}`);
    const rp = await post('/api/devices/ur-r/rest-push', { enabled: true, url: 'http://127.0.0.1:9/ingest?apikey=zz9', interval_s: 60, format: 'native', verify_tls: true, headers: {} });
    if (rp.status !== 200) throw new Error(`rest push ur-r: ${JSON.stringify(rp.body)}`);
  }
  await page.evaluate(() => window.app.renderDevicesList());
  await page.waitForTimeout(600);
  menu = await rowMenu(page, 'ur-r');
  await page.click(`${menu} [data-ddv-act="delete"]`);
  await page.waitForSelector('#toast-container .undo-toast');
  const tK = await toast(page).innerText();
  check('K: the toast says its REST push resumes with Undo, with its host (never the key)', /REST push/.test(tK) && /127\.0\.0\.1:9/.test(tK) && !/zz9/.test(tK), tK);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(1000);
  const saidK = await page.locator('#toast-container .toast').allInnerTexts();
  check('K: the restored toast says it resumed', saidK.some(x => /restored/i.test(x) && /REST push/.test(x)), saidK);
  await post('/api/devices/ur-r/rest-push', { enabled: false, url: '' });
  check('I–K: no page errors', page.errs.length === 0, page.errs);
  await page.context().close();

  // ── L · malformed kept devices (M2, second review R1): a bad field, a
  // bad connection, a device that is a list, a file that is a list ──
  if (process.env.FX_CFG) {
    const BAD = {
      'ur-bad': { device: { id: 'ur-bad', connection: { protocol: 'tcp', host: '127.0.0.1' }, commands: 5 }, deleted_ts: 1 },
      'ur-bad-conn': { device: { id: 'ur-bad-conn', connection: 'x' }, deleted_ts: 1 },
      'ur-bad-list': { device: [1, 2], deleted_ts: 1 },
      'ur-bad-top': [{ device: { id: 'ur-bad-top' } }],
    };
    for (const [id, body] of Object.entries(BAD)) {
      const dir = `${process.env.FX_CFG}/devices/${id}`;
      mkdirSync(dir, { recursive: true });
      writeFileSync(`${dir}/device.json`, JSON.stringify(body));
      chmodSync(dir, 0o777); chmodSync(`${dir}/device.json`, 0o666);
    }
    const plain = await api.get('/api/devices/restorable');
    check('L: the plain list stands with them', plain.status() === 200, plain.status());
    page = await open();
    await page.evaluate(() => window.app.openRestoreDialog());
    await page.waitForSelector('#restoreModal.active');
    await page.waitForSelector('#restoreList [data-restore-row="ur-bad"]', { timeout: 8000 }).catch(() => {});
    check('L: the dialog loads with every malformed kept device in it', (await Promise.all(Object.keys(BAD).map(id =>
      page.locator(`#restoreList [data-restore-row="${id}"]`).count()))).every(n => n === 1)
          && await page.locator('#restoreList [data-restore-row]').count() >= 5, await page.locator('#restoreList').innerText());
    for (const id of Object.keys(BAD)) {
      const row = page.locator(`#restoreList [data-restore-row="${id}"]`);
      const txt = await row.innerText().catch(() => '');
      check(`L ${id}: its row says why, in plain words`, await row.locator('.restore-problem').count() === 1 && !/Error|object/.test(txt), txt);
      await row.locator(`[data-forget-id="${id}"]`).click();
      await page.waitForTimeout(1200);
      check(`L ${id}: forgotten from the dialog`, await page.locator(`#restoreList [data-restore-row="${id}"]`).count() === 0
            && !(await get('/api/devices/restorable')).devices.some(d => d.id === id));
    }
    check('L: no page errors', page.errs.length === 0, page.errs);
    await page.context().close();
  } else {
    check('L: FX_CFG given (the fixture config dir), to plant a malformed kept device', false, 'FX_CFG not set');
  }
} catch (e) {
  check(`no exception: ${e.message}`, false, e.stack);
} finally {
  await browser.close();
  await cleanup();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
