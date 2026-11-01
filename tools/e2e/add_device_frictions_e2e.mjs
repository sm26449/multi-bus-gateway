// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The Add Device frictions of the browser inventory (L3-UX-12, ADR-0032
// L3.11), each as a user meets it:
// 1  the three other ways in are above the model cards, seen without
//    scrolling (1440×950)
// 2  Ethernet: Master is chosen for the user — its only possible role — and
//    the step says why; typing the address does not clear it
// 3  a chip counts the models its list shows: with a search, and on the
//    "start from the connection" way, where the list is what the connection
//    speaks
// 4  the Check of a device on a bus a running device reads (em1 at
//    127.0.0.1:15502, timeout 1 s) is not refused with a raw
//    "connection.timeout: …" line: the bus's timeout is taken, and said
// 5  the result screen has one Done
// 6  the first-run "Connect your first device" offer does not open on a
//    gateway that has devices (none of which answers); it still opens on
//    one that has none
//
// Against a throwaway gateway of the qa fixture, login on (127.0.0.1 only):
//   MBG_FX_NAME=mbg-fx-l311c tools/e2e/fixture_up.sh qa 18411 --login e2e-pass
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18411 MBG_PASS=e2e-pass node tools/e2e/add_device_frictions_e2e.mjs
//   docker rm -f mbg-fx-l311c
// It adds af-* devices and removes them (and their kept settings) at the end.
import { chromium, request as pwRequest } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import * as AD from './lib/add-device.mjs';

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
const IDS = ['af-eth', 'af-meter'];
async function cleanup() {
  for (const id of IDS) await api.delete(`/api/devices/${id}`).catch(() => {});
  for (const id of IDS) await api.delete(`/api/devices/restorable/${id}`).catch(() => {});
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(opts = {}) {
  const ctx = await browser.newContext({ storageState: state, viewport: opts.viewport || { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => { if (!/Failed to fetch/.test(String(e))) page.errs.push(String(e)); });
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|40[134]|409|412|422|429|Failed to fetch/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  if (opts.route) await opts.route(page);
  await page.goto(BASE + '/#/devices');
  await page.waitForLoadState('load');
  await page.waitForSelector('#devicesList .device-row', { timeout: 15000 }).catch(() => {});
  return page;
}
const visibleCards = page => page.locator('#adResults [role="option"]').count();
const chipCount = (page, id) => page.locator(`#adChips [data-ad-type="${id}"] .find-chip-n`).innerText().then(Number);
const chipIds = page => page.locator('#adChips [role="radio"]').evaluateAll(els => els.map(e => e.dataset.adType));

try {
  check('logged in (the fixture\'s login)', login.ok());
  await cleanup();

  // ── 6 · the first-run offer (its grace is 10 s) ─────────────────────────
  let page = await open();
  await sleep(13000);
  check('6: a gateway with devices (none answering): no first-run offer', await page.locator('#firstRunModal.active').count() === 0);
  await page.context().close();
  page = await open({ route: async p => {
    await p.route('**/api/status', async r => {
      const res = await r.fetch();
      const j = await res.json();
      j.devices = [];
      if (j.modbus) j.modbus.last_success_ts = null;
      await r.fulfill({ response: res, json: j });
    });
  } });
  await page.waitForSelector('#firstRunModal.active', { timeout: 25000 }).catch(() => {});
  check('6: a gateway with none: the offer opens', await page.locator('#firstRunModal.active').count() === 1);
  await page.context().close();

  page = await open();
  // ── 1 · the other ways in, above the cards ──────────────────────────────
  await AD.openAdd(page);
  await page.waitForSelector('#adResults [role="option"]');
  const ways = await page.locator('.ad-ways').boundingBox();
  const cards = await page.locator('#adResults').boundingBox();
  check('1: the three ways come before the model cards', ways && cards && ways.y < cards.y, { ways, cards });
  check('1: …seen without scrolling', ways && ways.y + ways.height <= 950, ways);
  check('1: three of them', await page.locator('.ad-ways [data-ad-way]').count() === 3);
  const sum = await page.locator('#addDevSummary').innerText().catch(() => '');
  check('1: the footer does not send the user below', !/below/.test(sum), sum);

  // ── 3 · the chips count what the list shows ─────────────────────────────
  const anyAll = await chipCount(page, '');
  check('3: Any counts every card', anyAll === await visibleCards(page), [anyAll, await visibleCards(page)]);
  await page.fill('#adQ', 'eastron');
  await page.waitForTimeout(300);
  const ids = await chipIds(page);
  for (const id of ids) {
    const n = await chipCount(page, id);
    await page.click(`#adChips [data-ad-type="${id}"]`);
    await page.waitForTimeout(150);
    const shown = await visibleCards(page);
    check(`3: "eastron": chip ${id || 'Any'} says ${n}, its list shows ${shown}`, n === shown);
  }
  await page.click('#adChips [data-ad-type=""]');
  await page.fill('#adQ', '');

  // ── 2 · Ethernet: Master chosen, and why ────────────────────────────────
  await page.click('#adResults [data-ad-tpl="eastron_sdm120"]');
  await AD.next(page);
  await AD.settled(page);
  await AD.answer(page, 'PortKind', 'ethernet');
  await page.waitForSelector('#csAddRole-poll', { state: 'attached' });
  check('2: Ethernet: Master chosen', await page.isChecked('#csAddRole-poll'));
  check('2: …Listen-only refused there', (await page.getAttribute('#csAddRole-listen', 'aria-disabled')) === 'true'
        || await page.isDisabled('#csAddRole-listen'));
  const note = await page.locator('#csAddRoleNote').innerText().catch(() => '');
  check('2: …and the step says why (a Modbus TCP server has no bus traffic to hear)', /Modbus TCP server/.test(note), note);
  check('2: not "Nobody claims this bus yet"', !/Nobody claims/.test(await page.locator('#csAddRoleQ').innerText().catch(() => '')));
  await AD.fill(page, 'Host', '127.0.0.1');
  await AD.fill(page, 'Port', '15502');
  await AD.fill(page, 'Unit', '7');
  await page.waitForTimeout(400);
  check('2: typing the address (the bus em1 polls) keeps Master', await page.isChecked('#csAddRole-poll'));

  // ── 4 · the Check through em1's connection ──────────────────────────────
  check('4: the timeout is the form\'s default (3 s), not the bus\'s', await page.inputValue('#csAddTimeout') === '3', await page.inputValue('#csAddTimeout'));
  // every text the dialog's live region says (review m3: the change is announced)
  await page.evaluate(() => {
    window.__said = [];
    const live = document.getElementById('addDevLive');
    new MutationObserver(() => { if (live.textContent) window.__said.push(live.textContent); }).observe(live, { childList: true, characterData: true, subtree: true });
  });
  let st = await AD.next(page);
  check('4: → Check', st === 'check', st);
  const c = await AD.waitCheck(page);
  const chk = await page.locator('#adCheck').innerText();
  check('4: no raw "connection.timeout:" line', !/connection\.timeout/.test(chk), chk);
  check('4: no doubled quote', !/''s/.test(chk), chk);
  check('4: the bus\'s timeout taken, and said with whose connection it is', /em1|Cabinet meter/.test(chk) && /1 s/.test(chk), chk);
  check('4: the check ran (or ended), not refused for the timeout', c.state !== 'refused' || !/timeout/.test(c.refusal || ''), c);
  const said = await page.evaluate(() => window.__said || []);
  check('4 (m3): the change is announced in the live region', said.some(x => /Cabinet meter|em1/.test(x) && /timeout/.test(x)), said);
  // back on Connection: the value taken is marked, with whose it is
  await page.click('#addDevRail [data-shell-step="connection"]');
  await page.waitForSelector('#csAddTimeout');
  await page.waitForTimeout(500);
  check('4 (m3): the field taken from the bus is marked', await page.getAttribute('#csAddTimeout', 'data-adopted') !== null
        && await page.inputValue('#csAddTimeout') === '1', await page.inputValue('#csAddTimeout'));
  const mark = await page.locator('[data-adopted-note]').innerText().catch(() => '');
  check('4 (m3): …and says from where', /Cabinet meter|em1/.test(mark), mark);
  st = await AD.next(page);
  if (st === 'check') { await AD.waitCheck(page); st = await AD.next(page); }
  check('4: → Name & save', st === 'save', st);
  await page.fill('#adName', 'AF eth');
  await page.locator('#adName').dispatchEvent('input');
  await page.fill('#adId', 'af-eth');
  await page.locator('#adId').dispatchEvent('input');
  await AD.create(page, {}).catch(() => {});

  // ── 5 · one Done ────────────────────────────────────────────────────────
  await page.waitForSelector('#adResult', { timeout: 20000 }).catch(() => {});
  const dones = await page.locator('#addDeviceModal button:visible').evaluateAll(els =>
    els.filter(b => /^\s*Done\s*$/.test(b.textContent || '')).map(b => b.id));
  check('5: the result screen has one Done', dones.length === 1 && dones[0] === 'adResDone', dones);
  const stored = ((await (await api.get('/api/devices/af-eth')).json()) || {}).device;
  check('4: the device keeps the bus\'s timeout', stored && +(stored.connection || {}).timeout === 1, stored && stored.connection);
  await page.click('#adResDone');
  check('5: Done closes', await page.locator('#addDeviceModal.active').count() === 0);

  // ── 3 · by connection: the chips count what the connection speaks ──────
  await AD.openAdd(page);
  await page.click('.ad-ways [data-ad-way="connection"]');
  await AD.settled(page);
  await AD.answer(page, 'PortKind', 'ethernet');
  await AD.fill(page, 'Host', '127.0.0.1');
  await AD.fill(page, 'Port', '15599');
  await AD.fill(page, 'Unit', '9');
  st = await AD.next(page);
  check('3: by connection → the model step', st === 'what', st);
  await page.waitForSelector('#adResults [role="option"]');
  for (const id of await chipIds(page)) {
    const n = await chipCount(page, id);
    await page.click(`#adChips [data-ad-type="${id}"]`);
    await page.waitForTimeout(150);
    const shown = await visibleCards(page);
    check(`3: by connection: chip ${id || 'Any'} says ${n}, its list shows ${shown}`, n === shown);
  }
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  check('no page errors', page.errs.length === 0, page.errs);
  await page.context().close();
} catch (e) {
  check(`no exception: ${e.message}`, false, e.stack);
} finally {
  await browser.close();
  await cleanup();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
