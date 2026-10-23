// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Nothing is deleted while something uses it — as a user meets it: the
// "Used by" section on a device, an installation and a template, one row and
// one count per thing (a rule that reads and commands is one rule), each a
// link to the exact place to fix it; Delete held back with the reason said
// and asked again before saying so; Disable instead, saying what it does to
// the rules that read or command it; a device with components deleted only
// with its id typed; a delete that loses a race shows the gateway's list; the
// list draws at once and fills its badges later; a viewer reads the list and
// is offered no delete; the Romanian words agree with an installation.
//
// Against the throwaway gateway of the installation_safety fixture, writes
// on (every address the container's own loopback):
//   tools/e2e/fixture_up.sh safety 18087 --writes
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 OUT=<dir> node tools/e2e/used_by_e2e.mjs
// It adds ub-* devices and rules ub-*, and removes them at the end.
import { chromium } from 'playwright';
import { watchDialogs, openDialog } from './lib/dialogs.mjs';
import { deviceAction } from './lib/device-page.mjs';

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
const tcp = (id, port) => ({ id, name: id.toUpperCase().replace('-', ' '), template: 'eastron_sdm630',
  connection: { protocol: 'tcp', host: '127.0.0.1', port, unit_id: 1 } });
const rule = (id, label, signal, target = 'sunfield-u2') => ({ id, label, mode: 'shadow',
  target: { device: target, command: 'power_limit' }, kind: 'steps',
  signal, signal_valid: { min: 100, max: 350 }, stale_after_s: 60,
  steps: [{ at: 250, value: 80 }], release_below: 248, normal: { value: 100 },
  params: { revert_s: 0 }, timing: { every_s: 2 }, on_stale: 'safe', on_disable: 'safe' });
const calc = (dev, rows) => api(`/api/devices/${dev}/calculated`, { method: 'POST', body: JSON.stringify({ calculated: rows }) });
const A2 = { name: 'double_a', expr: 'ub-a.power_active_total * 2', unit: 'W' };

async function cleanup() {
  for (const r of ['ub-ov', 'ub-rd', 'ub-bat']) await api(`/api/rules/${r}`, { method: 'DELETE' });
  await calc('ub-b', []);
  await api('/api/devices/ub-c/energy-meter', { method: 'PUT', body: '{}' });
  for (const id of ['ub-b', 'ub-c', 'ub-a', 'ub-d', 'ub-gx']) await api(`/api/devices/${id}`, { method: 'DELETE' });
}

async function setup() {
  await cleanup();
  for (const [d] of [[tcp('ub-a', 15600)], [tcp('ub-b', 15601)], [tcp('ub-c', 15602)], [tcp('ub-d', 15603)]]) {
    const r = await api('/api/devices', { method: 'POST', body: JSON.stringify(d) });
    if (r.status !== 200) throw new Error(`create ${d.id}: ${JSON.stringify(r.body)}`);
  }
  await calc('ub-b', [A2]);
  await api('/api/devices', { method: 'POST', body: JSON.stringify({ id: 'ub-gx', name: 'UB GX', template: 'victron_gx_mqtt',
    connection: { protocol: 'mqtt', broker: '127.0.0.1', port: 1883, topic: 'N/x/#', vars: { portal: 'x' } } }) });
  await api('/api/devices/ub-gx/components', { method: 'POST', body: JSON.stringify({ type: 'battery', instance: '288' }) });
  // one rule that reads AND commands sunfield-u1, one that reads ub-a
  for (const r of [rule('ub-ov', 'OV ub test', 'sunfield-u1.voltage_l1_n', 'sunfield-u1'), rule('ub-rd', 'Reads A', 'ub-a.voltage_l1_n')]) {
    const x = await api('/api/rules', { method: 'POST', body: JSON.stringify(r) });
    if (x.status !== 200) throw new Error(`rule ${r.id}: ${JSON.stringify(x.body)}`);
  }
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
  if (opts.slowCounts) await page.route('**/api/dependents', async r => { await new Promise(res => setTimeout(res, 2500)); r.continue(); });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const usedBy = page => page.locator('#devUsedBy');
const dialogOpen = async page => !!(await openDialog(page));
const activeTab = page => page.evaluate(() => document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab);
const focusIn = (page, sel) => page.evaluate(s => !!document.activeElement?.closest(s), sel);
const closeDialog = page => page.evaluate(() => window.app.closeModal('appDialog'));

try {
  await setup();

  // ── the list draws at once and fills its badges when they arrive ──
  const slow = await open(null, { slowCounts: true });
  const t0 = Date.now();
  await slow.evaluate(() => window.app.navigateTo('devices'));
  await slow.locator('.device-row', { hasText: 'ub-a' }).first().waitFor();
  const drawnIn = Date.now() - t0;
  const early = await slow.locator('.device-row', { hasText: 'ub-a' }).first().innerText();
  check('list: drawn before the counts arrive', drawnIn < 2000 && !/used by/i.test(early), `${drawnIn} ms`);
  await slow.waitForFunction(() => /used by/i.test(document.querySelector('[data-ub-slot="ub-a"]')?.textContent || ''), null, { timeout: 8000 });
  check('list: the badge is filled when they arrive', true);
  await slow.close();

  const page = await open(null);
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForFunction(() => /used by/i.test(document.querySelector('[data-ub-slot="ub-a"]')?.textContent || ''));

  // ── one thing, one count: a rule that reads and commands is one rule ──
  const u1 = await page.locator('[data-ub-slot="sunfield-u1"]').innerText();
  check('list: sunfield-u1 used by 1 (one rule)', /used by 1\b/i.test(u1), u1);
  const a = await page.locator('[data-ub-slot="ub-a"]').innerText();
  check('list: ub-a used by 2 (a calculated value, a rule)', /used by 2\b/i.test(a), a);
  const rowDel = page.locator('[data-ub-del="ub-a"]');
  check('list: its Delete is held back, with the reason', (await rowDel.getAttribute('aria-disabled')) === 'true'
        && /cannot be deleted/i.test(await rowDel.getAttribute('aria-label') || ''), await rowDel.getAttribute('aria-label'));
  check('list: a parent whose component is read shows nothing yet', !(await page.locator('[data-ub-slot="ub-gx"]').innerText()));

  await page.evaluate(() => window.app.openDeviceDetail('sunfield-u1'));
  await usedBy(page).locator('.ub-list').first().waitFor();
  const u1page = await usedBy(page).innerText();
  check('unit page: one row for the rule, saying both what it does',
        (await usedBy(page).locator('.ub-list li').count()) === 1 && /commands it · reads it in signal/.test(u1page), u1page);
  check('unit page: header, section and list agree (1)', /used by 1\b/i.test(await page.locator('#ddvUsedByBadge').innerText())
        && (await usedBy(page).locator('[data-ub-total]').innerText()) === '1');
  check('unit page: its own words (no Delete, no Disable offered there)', /installation cannot drop it/.test(u1page) && !/disable/i.test(u1page), u1page);
  check('only the summary line is live', (await usedBy(page).locator('[aria-live]').count()) === 1
        && (await usedBy(page).locator('[aria-live]').evaluate(e => e.matches('[data-ub-status]'))));

  // ── the device page: section, header, Delete held, Disable instead ──
  await page.evaluate(() => window.app.openDeviceDetail('ub-a'));
  await usedBy(page).locator('.ub-list').first().waitFor();
  const txt = await usedBy(page).innerText();
  check('device page: names the calculated value and the rule', /Calculated values on other devices/.test(txt) && /double_a/.test(txt) && /Reads A/.test(txt), txt);
  check('device page: the header counts things (2)', /used by 2\b/i.test(await page.locator('#ddvUsedByBadge').innerText()));
  const del = page.locator('#ddvDeleteBtn');
  check('device page: Delete held back, the reason said to a screen reader',
        (await del.getAttribute('aria-disabled')) === 'true' && /cannot be deleted/i.test(await page.locator('#ddvDeleteWhy').textContent()));
  await deviceAction(page, '#ddvDeleteBtn');   // aria-disabled: still clickable, it shows why
  await page.waitForTimeout(600);
  check('Delete while used opens no dialog, focus goes to "Used by"', !(await dialogOpen(page)) && await focusIn(page, '#devUsedBy'));
  if (OUT) await page.screenshot({ path: `${OUT}/usedby-device.png` });

  // ── Disable instead says what it does to the rules ──
  await deviceAction(page, '#ddvDisableInstead');
  await page.waitForSelector('#appDialog.active');
  const dd = await openDialog(page);
  const effects = await page.locator('#appDialogDetails').innerText();
  check('Disable instead: names the rule that reads it and its stale policy',
        /Rule Reads A: its input goes stale, then it sends its safe values/.test(effects), effects);
  check('Disable instead: names the calculated value that stops', /UB B: its calculated value stops updating/.test(effects), effects);
  check('Disable instead: never "everything stays as it is", not a red dialog',
        !/stays as it is/i.test(dd.message) && !(await page.locator('#appDialogOk.btn-danger').count()), dd.message);
  await page.click('#appDialogCancel');
  await page.waitForTimeout(200);
  // the item lived in the ⋯ menu, now closed: the focus goes back to ⋯
  check('Disable instead: Cancel gives focus back to the menu that opened it', (await page.evaluate(() => document.activeElement?.id)) === 'ddvActions');

  // ── a link opens the exact place: the calculated row, by keyboard ──
  const link = usedBy(page).locator('a.ub-item', { hasText: 'UB B' });
  check('the link has an accessible name', /Open device UB B/.test(await link.getAttribute('aria-label') || ''), await link.getAttribute('aria-label'));
  await link.focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => window.app._devDetail?.id === 'ub-b');
  await page.waitForSelector('.calc-row[data-name="double_a"].st-highlight', { timeout: 8000 }).catch(() => {});
  check('Enter opens ub-b on Calculated, at the row double_a', (await activeTab(page)) === 'calculated'
        && await page.locator('.calc-row[data-name="double_a"].st-highlight').count() === 1);

  // ── a rule link opens its editor at the field; Esc returns to its card ──
  await page.evaluate(() => window.app.openDeviceDetail('ub-a'));
  await usedBy(page).locator('a.ub-item', { hasText: 'Reads A' }).click();
  await page.waitForSelector('#ruleModal.active', { timeout: 8000 }).catch(() => {});
  await page.waitForFunction(() => document.activeElement?.id === 'rlSignal', null, { timeout: 3000 }).catch(() => {});
  check('the rule link opens the editor at its signal', await page.locator('#ruleModal.active').count() === 1
        && (await page.evaluate(() => document.activeElement?.id)) === 'rlSignal');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  check('Esc from the editor returns to the rule\'s card', await focusIn(page, '[data-rule="ub-rd"]'));

  // ── a held Delete asks again: the page was older than the gateway ──
  await page.evaluate(() => window.app.openDeviceDetail('ub-a'));
  await usedBy(page).locator('.ub-list').first().waitFor();
  await api('/api/rules/ub-rd', { method: 'DELETE' });
  await calc('ub-b', []);
  await deviceAction(page, '#ddvDeleteBtn');
  await page.waitForSelector('#appDialog.active', { timeout: 6000 }).catch(() => {});
  check('a held Delete asked again: free now, so the dialog opens', await dialogOpen(page));
  await closeDialog(page);

  // ── Disable instead: focus lands on the page, references kept ──
  await calc('ub-b', [A2]);
  await page.evaluate(() => window.app.openDeviceDetail('ub-a'));
  await page.waitForFunction(() => document.getElementById('ddvDisableInstead') && !document.getElementById('ddvDisableInstead').hidden);
  await deviceAction(page, '#ddvDisableInstead');
  await page.waitForSelector('#appDialog.active');
  await page.click('#appDialogOk');
  await page.waitForFunction(() => !document.getElementById('appDialog').classList.contains('active'));
  await page.waitForTimeout(800);
  const adev = (await api('/api/devices')).body.devices.find(d => d.id === 'ub-a');
  const kept = ((await api('/api/devices/ub-b/calculated')).body.calculated || []).some(c => c.name === 'double_a');
  check('Disable instead: disabled, the calculated value that reads it kept', adev?.enabled === false && kept);
  check('Disable instead: focus on the redrawn page, not <body>', await focusIn(page, '#deviceDetailView h2'));

  // ── a device with components: its id typed, what goes with it said ──
  await page.evaluate(() => window.app.openDeviceDetail('ub-gx'));
  await usedBy(page).locator('.ub-empty').waitFor();
  await deviceAction(page, '#ddvDeleteBtn');
  await page.waitForSelector('#appDialog.active');
  const details = await page.locator('#appDialogDetails').innerText();
  check('the dialog names the component going with it', /component goes with it/.test(details) && /288/.test(details), details);
  check('and asks for the id typed (OK locked)', (await openDialog(page)).okDisabled && await page.locator('#appDialogF0').isVisible());
  await closeDialog(page);

  // ── a component that is read: its row and its parent say so ──
  const bat = await api('/api/rules', { method: 'POST', body: JSON.stringify(rule('ub-bat', 'Reads battery', 'ub-gx-battery-288.soc')) });
  await page.evaluate(() => window.app.closeDeviceDetail(true));
  await page.waitForFunction(() => /used by/i.test(document.querySelector('[data-ub-slot="ub-gx"]')?.textContent || ''), null, { timeout: 8000 }).catch(() => {});
  check('list: a parent counts what uses its component, and holds Delete',
        bat.status === 200 && /used by 1\b/i.test(await page.locator('[data-ub-slot="ub-gx"]').innerText())
        && (await page.locator('[data-ub-del="ub-gx"]').getAttribute('aria-disabled')) === 'true');
  await api('/api/rules/ub-bat', { method: 'DELETE' });

  // ── a race inside the dialog: something starts using it before OK ──
  await page.evaluate(() => window.app.openDeviceDetail('ub-c'));
  await usedBy(page).locator('.ub-empty').waitFor();
  await deviceAction(page, '#ddvDeleteBtn');
  await page.waitForSelector('#appDialog.active');
  await calc('ub-b', [A2, { name: 'from_c', expr: 'ub-c.power_active_total', unit: 'W' }]);
  await page.click('#appDialogOk');
  await page.waitForTimeout(900);
  const toast = await page.locator('.toast').allInnerTexts().then(x => x.join(' | ')).catch(() => '');
  check('the race: the gateway\'s list is shown, said in words', /UB B/.test(await usedBy(page).innerText()) && /still in use/i.test(toast), toast);
  check('the race: focus on "Used by", not on Delete', await focusIn(page, '#devUsedBy'));
  check('the race: Delete now held back too', (await page.locator('#ddvDeleteBtn').getAttribute('aria-disabled')) === 'true');
  check('ub-c still exists', (await api('/api/devices')).body.devices.some(x => x.id === 'ub-c'));

  // ── an installation: one rule, through its unit; a group link ──
  await page.evaluate(() => window.app.openEndpointDetail('sunfield'));
  await page.locator('#plUsedBy .ub-list').first().waitFor();
  const it = await page.locator('#plUsedBy').innerText();
  check('installation: each rule once, through its unit', (await page.locator('#plUsedBy li', { hasText: 'OV ub test' }).count()) === 1
        && /through sunfield-u1/.test(await page.locator('#plUsedBy li', { hasText: 'OV ub test' }).innerText()), it);
  check('installation: Delete held back', (await page.locator('#plDeleteBtn').getAttribute('aria-disabled')) === 'true');
  await page.locator('#plDeleteBtn').click({ force: true });
  await page.waitForTimeout(500);
  check('installation: Delete while used opens no dialog', !(await dialogOpen(page)));

  // ── a template: card and View count the same things; a group link ──
  await page.evaluate(() => window.app.navigateTo('templates'));
  await page.locator('[data-tm-view="fronius_sunspec_inverter"]').waitFor();
  const card = await page.locator('.settings-card', { has: page.locator('[data-tm-view="fronius_sunspec_inverter"]') }).innerText();
  await page.evaluate(() => window.app.viewTemplate('fronius_sunspec_inverter'));
  await page.locator('#tmUsedBy .ub-list').first().waitFor();
  const viewN = await page.locator('#tmUsedBy [data-ub-total]').innerText();
  check('template: the card and the View count the same (1)', /Used by 1\b/.test(card) && viewN === '1', `${card} / ${viewN}`);
  await page.locator('#tmUsedBy a.ub-item').first().click();
  await page.waitForSelector('[data-endpoint-page] [data-group].st-highlight', { timeout: 8000 }).catch(() => {});
  check('a template\'s installation link opens the group that uses it', !(await page.locator('#tmViewModal.active').count())
        && await page.locator('[data-endpoint-page] [data-group].st-highlight').count() === 1);

  check('no native dialog fired', page.dlg.native === 0, String(page.dlg.native));
  check('no page errors (admin)', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── a viewer reads the list, is offered no delete, never lands on a hidden tab ──
  await api('/api/devices/ub-c/energy-meter', { method: 'PUT', body: JSON.stringify({ device: 'ub-d', power: 'power_active_total' }) });
  const v = await open('viewer');
  await v.evaluate(() => window.app.openDeviceDetail('ub-c'));
  await v.locator('#devUsedBy .ub-list').first().waitFor();
  check('viewer: sees what uses it', /UB B/.test(await v.locator('#devUsedBy').innerText()));
  // the ⋯ menu holds only administration on a device: a viewer is offered no menu at all
  check('viewer: no Delete, no Disable instead (no ⋯ menu offered)', !(await v.locator('#ddvActions').isVisible())
        && !(await v.locator('#ddvDeleteBtn').isVisible()) && !(await v.locator('#ddvDisableInstead').isVisible()));
  await v.evaluate(() => window.app.openDeviceDetail('ub-d'));
  await v.locator('#devUsedBy a.ub-item').first().waitFor();
  await v.locator('#devUsedBy a.ub-item').first().click();
  await v.waitForFunction(() => window.app._devDetail?.id === 'ub-c');
  await v.waitForTimeout(300);
  const tabShown = await v.evaluate(() => {
    const a = document.querySelector('#deviceWsTabs .config-main-tab.active');
    return a && a.offsetParent !== null;
  });
  check('viewer: a meter link lands on a tab they can see', tabShown, await activeTab(v));
  check('no page errors (viewer)', v.errs.length === 0, v.errs.join(' | '));
  await v.close();

  // ── Romanian: an installation's words are feminine ──
  const ro = await open(null);
  await ro.evaluate(() => window.app.setLanguage('ro', false));
  await ro.evaluate(() => window.app.openEndpointDetail('sunfield'));
  await ro.locator('#plUsedBy .ub-list').first().waitFor();
  const roTxt = await ro.locator('#plUsedBy').innerText();
  check('ro: "Folosită de", "nu poate fi ștearsă"', /Folosită de/.test(roTxt) && /ștearsă/.test(roTxt), roTxt);
  await ro.evaluate(() => window.app.setLanguage('en', false));
  await ro.close();

  // ── phone width, dark theme: the section fits ──
  const m = await open(null, { viewport: { width: 390, height: 844 }, theme: 'dark' });
  await m.evaluate(() => window.app.openDeviceDetail('ub-c'));
  await m.locator('#devUsedBy .ub-list').first().waitFor();
  const fit = await m.evaluate(() => {
    const el = document.getElementById('devUsedBy');
    return el.scrollWidth <= el.clientWidth + 1 && document.documentElement.scrollWidth <= window.innerWidth + 1;
  });
  check('phone: "Used by" fits, no sideways scroll', fit);
  if (OUT) await m.locator('#devUsedBy').screenshot({ path: `${OUT}/usedby-phone-dark.png` });
  check('no page errors (phone)', m.errs.length === 0, m.errs.join(' | '));
  await m.close();
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await cleanup();
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
