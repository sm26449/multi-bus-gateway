// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The page's own confirm / ask dialog (ui/js/app-dialogs.js), driven by
// keyboard and mouse as a user does — no _dialogAnswer here: the typed
// confirmation, Escape and focus return, Enter on a red dialog's Cancel, the
// Tab trap, a number that is not one, a decimal comma, a refusal said inside
// the dialog, the API key asked from inside a dialog's request, the dark
// theme, no native dialog anywhere, and no write control a viewer can see
// unmarked. Against the throwaway gateway of unchanged_save_e2e.mjs (its
// fixture); every write is answered by a route, nothing is changed.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 OUT=<dir> node tools/e2e/dialogs_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs, openDialog } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async path => (await fetch(BASE + path)).json();

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(role, theme) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 }, colorScheme: theme || 'light' });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.dlg.manual = true;
  if (role) await page.route('**/api/auth/status', r => r.fulfill({ json: { enabled: true, role, modules: [] } }));
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const active = page => page.evaluate(() => document.activeElement && (document.activeElement.id || document.activeElement.dataset.action || document.activeElement.tagName));
const inDialog = page => page.evaluate(() => !!document.activeElement?.closest('#appDialog'));
const isOpen = async page => !!(await openDialog(page));

try {
  const page = await open(null);
  // every write below is answered here: nothing reaches the gateway
  const sent = [];
  let refuse = false;
  let keyAsked = 0;
  const dryRuns = [];
  await page.route(/\/api\/(rules\/[^/]+\/(clamp|override)|endpoints\/[^/?]+)(\?dry_run=true)?$/, async r => {
    const req = r.request();
    if (req.method() === 'GET') return r.continue();
    // a delete asks the gateway first: that question goes through, unanswered here
    if (/dry_run=true/.test(req.url())) { dryRuns.push(req.url()); return r.continue(); }
    sent.push({ url: req.url(), method: req.method(), body: req.postDataJSON?.() ?? null, key: req.headers()['x-api-key'] || '' });
    if (/override/.test(req.url()) && !req.headers()['x-api-key']) { keyAsked++; return r.fulfill({ status: 401, json: { detail: 'API key required' } }); }
    if (refuse) return r.fulfill({ status: 409, json: { detail: { errors: ['refused by the test'] } } });
    return r.fulfill({ json: { status: 'ok' } });
  });

  // ── typed confirmation: deleting an installation nothing uses (bank) ──
  await page.evaluate(() => window.app.navigateTo('devices'));
  const del = page.locator('#devicesListView [data-action="deleteEndpointUi"][data-args=\'["bank"]\']').first();
  await del.waitFor();
  await del.focus();
  await del.click();
  await page.waitForSelector('#appDialog.active');
  let d = await openDialog(page);
  check('typed: the gateway was asked first', dryRuns.some(u => /endpoints\/bank\?dry_run=true$/.test(u)), JSON.stringify(dryRuns));
  check('typed: names the installation, OK locked', /bank/.test(d.title) && d.okDisabled, JSON.stringify(d));
  await page.waitForTimeout(80);
  check('typed: focus is in the box to type in', /appDialogF/.test(await active(page)), await active(page));
  await page.keyboard.type('b');
  check('typed: a partial id keeps OK locked', (await openDialog(page)).okDisabled);
  await page.keyboard.press('Enter');
  check('typed: Enter with the wrong text does nothing', await isOpen(page) && !sent.length);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(80);
  check('Escape cancels', !(await isOpen(page)) && !sent.length);
  check('Escape: focus goes back to Delete', (await active(page)) === 'deleteEndpointUi', await active(page));
  await del.click();
  await page.waitForSelector('#appDialog.active');
  await page.waitForTimeout(80);
  await page.keyboard.type('bank');
  check('typed: the exact id unlocks OK', !(await openDialog(page)).okDisabled);
  for (let i = 0; i < 8; i++) await page.keyboard.press('Tab');
  check('Tab stays inside the dialog', await inDialog(page), await active(page));
  await page.click('#appDialogOk');
  await page.waitForTimeout(400);
  check('typed: OK deletes (the request is sent)', sent.some(s => s.method === 'DELETE' && /endpoints\/bank$/.test(s.url)), JSON.stringify(sent));
  sent.length = 0;

  // ── a red dialog: focus on Cancel, Enter there is Cancel ──
  await page.evaluate(() => window.app.navigateTo('rules'));
  await page.waitForSelector('[data-rule="ov-u1"]');
  const ruleDel = page.locator('[data-rule="ov-u1"] [data-action="deleteRule"]');
  await ruleDel.click();
  await page.waitForSelector('#appDialog.active');
  await page.waitForTimeout(80);
  check('red dialog: focus starts on Cancel', (await active(page)) === 'appDialogCancel', await active(page));
  check('red dialog: an armed rule names what happens to its target', /value=100|keeps the values/.test((await openDialog(page)).message), (await openDialog(page)).message);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);
  check('red dialog: Enter on Cancel cancels', !(await isOpen(page)) && (await api('/api/rules')).rules.some(r => r.id === 'ov-u1'));

  // ── a number that is not one, a decimal comma, a refusal ──
  await page.locator('[data-rule="ov-u1"] [data-action="openRuleClamp"]').click();
  await page.waitForSelector('#appDialog.active');
  await page.fill('#appDialogF0', 'abc');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);
  check('clamp "abc": said in the dialog, nothing sent', await isOpen(page) && /not a number/i.test(await page.locator('#appDialog').innerText()) && !sent.length,
        await page.locator('#appDialog').innerText());
  check('clamp "abc": the field is marked', (await page.getAttribute('#appDialogF0', 'aria-invalid')) === 'true');
  refuse = true;
  await page.fill('#appDialogF0', '2,5');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(400);
  d = await openDialog(page);
  check('clamp "2,5" is sent as 2.5', sent.some(s => s.body && s.body.max === 2.5 && s.body.expires_s === 7200), JSON.stringify(sent));
  check('a refusal stays in the dialog, in words', d && /refused by the test/.test(d.error), JSON.stringify(d));
  if (OUT) await page.screenshot({ path: `${OUT}/dialog-refused.png` });
  await page.click('#appDialogCancel');
  refuse = false;
  sent.length = 0;

  // ── the API key, asked from inside a dialog's request ──
  await page.locator('[data-rule="ov-u1"] [data-action="openRuleOverride"]').click();
  await page.waitForSelector('#appDialog.active');
  await page.fill('#appDialogF0', '30');
  await page.click('#appDialogOk');
  await page.waitForSelector('#appKeyDialog.active');
  check('a 401 inside a dialog asks for the key on top of it', keyAsked === 1 && await isOpen(page));
  await page.fill('#appKeyInput', 'k-test');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(500);
  check('the request goes again with the key, and the dialog closes', sent.some(s => s.key === 'k-test' && s.body?.seconds === 1800) && !(await isOpen(page)),
        JSON.stringify(sent));
  await page.evaluate(() => { try { localStorage.removeItem('mbg-api-key'); } catch (e) {} });

  // ── a request in flight: Escape does not cancel what is already sent ──
  const flight = await page.evaluate(async () => {
    let release;
    const gate = new Promise(r => { release = r; });
    const p = window.app.confirmAction({ title: 'slow', submit: () => gate.then(() => '') });
    await new Promise(r => setTimeout(r, 50));
    document.getElementById('appDialogOk').click();
    await new Promise(r => setTimeout(r, 50));
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    window.app.closeModal('appDialog');
    const stillOpen = document.getElementById('appDialog').classList.contains('active');
    release();
    await new Promise(r => setTimeout(r, 50));
    // an empty refusal (HTTP/2 has no status text) is still a refusal
    const err = document.getElementById('appDialogErr').textContent;
    window.app.closeModal('appDialog');
    return { stillOpen, err, answer: await p };
  });
  check('in flight: Escape and closeModal keep the dialog', flight.stillOpen, JSON.stringify(flight));
  check('an empty refusal is said, not taken as done', !!flight.err && flight.answer === false, JSON.stringify(flight));

  // ── plain decimals only, whole numbers where the step is 1 ──
  const nums = await page.evaluate(async () => {
    const out = {};
    for (const [k, step] of [['0x10'], ['1e3'], ['1.5', 1], ['3', 1]]) {
      const p = window.app.askValue({ title: 'n', step });
      const r = await window.app._dialogAnswer(k);
      out[`${k}/${step || ''}`] = r.open ? r.errors.join(',') : 'accepted';
      if (r.open) window.app.closeModal('appDialog');
      await p;
    }
    return out;
  });
  check('"0x10" and "1e3" are not numbers; 1.5 is not whole; 3 is',
        /number/i.test(nums['0x10/']) && /number/i.test(nums['1e3/']) && /whole/i.test(nums['1.5/1']) && nums['3/1'] === 'accepted',
        JSON.stringify(nums));

  // ── one dialog at a time ──
  const second = await page.evaluate(async () => {
    const a = window.app.confirmAction({ title: 'first' });
    const b = await window.app.confirmAction({ title: 'second' });
    window.app.closeModal('appDialog');
    return { b, first: await a };
  });
  check('a second dialog while one is open answers "no"', second.b === false && second.first === false, JSON.stringify(second));

  // ── no native dialog, no dialog left open, on every page ──
  for (const pg of ['dashboard', 'devices', 'templates', 'vmeters', 'rules', 'diagnostics', 'config', 'status']) {
    await page.evaluate(p => window.app.navigateTo(p), pg);
    await page.waitForTimeout(500);
  }
  check('no native dialog fired', page.dlg.native === 0, String(page.dlg.native));
  check('no dialog left open', !(await isOpen(page)));
  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── the dark theme: the dialog and the Diagnostics inputs ──
  const dark = await open(null, 'dark');
  await dark.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await dark.evaluate(() => window.app.navigateTo('diagnostics'));
  await dark.waitForSelector('#probeUnit', { state: 'attached' });
  await dark.waitForTimeout(400);
  const bg = await dark.evaluate(() => getComputedStyle(document.getElementById('probeUnit')).backgroundColor);
  check('dark: Diagnostics inputs are not white', bg !== 'rgb(255, 255, 255)', bg);
  if (OUT) await dark.screenshot({ path: `${OUT}/dark-diagnostics.png` });
  await dark.evaluate(() => { window.app.askValues({ title: 'Clamp ov-u1', message: 'A ceiling on what the rule may ask for',
    fields: [{ name: 'max', label: 'Ceiling', initial: 'abc' }, { name: 'hours', label: 'For how many hours', unit: 'h', min: 0, initial: 2 }] }); });
  await dark.waitForSelector('#appDialog.active');
  await dark.click('#appDialogOk');
  const dbg = await dark.evaluate(() => getComputedStyle(document.querySelector('#appDialog .modal-content')).backgroundColor);
  check('dark: the dialog is dark', dbg !== 'rgb(255, 255, 255)', dbg);
  if (OUT) await dark.screenshot({ path: `${OUT}/dark-dialog.png` });
  await dark.close();

  // ── a viewer sees no write control without a mark ──
  const viewer = await open('viewer');
  const WRITE = /\b(add|new|delete|remove|save|import|upload|duplicate|edit|arm|run|force|clamp|pause|restore|apply|forget|disable|enable)\b/i;
  const found = [];
  const VIEW_ONLY = ['diagPauseBtn'];
  for (const pg of ['devices', 'templates', 'vmeters', 'rules', 'diagnostics', 'config']) {
    await viewer.evaluate(p => window.app.navigateTo(p), pg);
    await viewer.waitForTimeout(700);
    found.push(...(await viewer.evaluate(([re, p, VIEW_ONLY]) => [...document.querySelectorAll('.page.active button, .page.active a.btn')]
      .filter(b => b.offsetParent !== null && !b.disabled)
      // tabs, and controls that only change the view (pausing the bus monitor's list)
      .filter(b => !b.closest('[role="tablist"], .config-main-tabs, .config-tabs') && !VIEW_ONLY.includes(b.id))
      .filter(b => new RegExp(re, 'i').test(`${b.textContent} ${b.title} ${b.getAttribute('aria-label') || ''}`))
      .map(b => `${p}: ${(b.id || b.dataset.action || b.textContent).trim().slice(0, 40)}`), [WRITE.source, pg, VIEW_ONLY])));
  }
  check('viewer: no unmarked write control is shown', !found.length, found.join(' | '));
  await viewer.close();
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
