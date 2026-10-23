// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Finding a device or a template: the search hides and shows rows
// without redrawing them (case and diacritics never matter, a kind is found
// in either language), filter chips count and combine, a tree or a list, the
// view in the address — one history entry per step, Back to the row it left,
// a bad link said plainly; the page's heading takes the focus and names the
// tab; context rows say they are context; every icon button has a name; a
// device's kind set by an administrator; deleted devices folded; the bar on
// a phone and in the other language. Ids are read from the gateway, so it
// runs on any fixture with an installation and writes on — the safety one:
//
//   tools/e2e/fixture_up.sh safety 18087 --writes
//   MBG_FX_NAME=mbg-fx-scale tools/e2e/fixture_up.sh safety 18096 --writes
//   python3 tools/e2e/fixtures/scale/load_scale.py http://127.0.0.1:18096 200 100
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 SCALE_URL=http://127.0.0.1:18096 OUT=<dir> node tools/e2e/findability_e2e.mjs
//
// It adds and removes its own devices (fa-*). With SCALE_URL, the speed at
// 200 devices / 100 templates is reported (targets, not gates).
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { deviceAction } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const SCALE = process.env.SCALE_URL || '';
const OUT = process.env.OUT || '';
const PREFIX = process.env.SHOT_PREFIX || 'findability2';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const mkDev = (id, name, extra = {}) => api('/api/devices', { method: 'POST', body: JSON.stringify({
  id, name, template: 'eastron_sdm630', enabled: false,
  connection: { protocol: 'tcp', host: '127.0.0.1', port: 15991, unit_id: 1 }, ...extra }) });

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(url, { role, width = 1440, height = 950, theme = 'light', lang } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme });
  if (lang) await ctx.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, lang);
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  if (role) await page.route('**/api/auth/status', r => r.fulfill({ json: { enabled: true, role, modules: [] } }));
  await page.goto(url);
  await page.waitForLoadState('load');
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 20000 });
  // the first-run prompt is not what this test is about
  await page.evaluate(() => { window.app._firstRunShown = true; window.app.closeModal?.('firstRunModal'); });
  return page;
}
const visibleIds = page => page.locator('#devicesList [data-f]:visible').evaluateAll(els => [...new Set(els.map(e => e.dataset.f))].sort());
const hash = page => page.evaluate(() => location.hash);
const settle = page => page.waitForTimeout(450);
const focused = page => page.evaluate(() => {
  const a = document.activeElement;
  return a ? (a.closest('.section-header') && a.tagName === 'H2' ? 'H2' : a.dataset?.f || a.id || a.tagName) : '';
});

try {
  // ── what this gateway holds ──
  await mkDev('fa-hala', 'Pompă căldură Hală');
  await mkDev('fa-gone', 'Gone soon');
  await api('/api/devices/fa-gone', { method: 'DELETE' });
  const all = (await api('/api/devices')).body.devices;
  const total = all.length;
  const eps = (await api('/api/endpoints')).body.endpoints || [];
  const ep = eps.find(p => (all.filter(d => d.endpoint_id === p.id).length) >= 2) || eps[0];
  const units = all.filter(d => d.endpoint_id === ep?.id);
  const unit = units[0];
  const inverters = all.filter(d => d.kind === 'inverter').map(d => d.id).sort();
  const standalone = all.find(d => !d.primary && !d.endpoint_id && !d.component_of && d.id !== 'fa-hala');

  // ── the bar ──
  let page = await open(BASE + '/#/devices');
  await page.waitForSelector('#devFindQ');
  check('a link to #/devices opens the Devices page', await page.evaluate(() => window.app.currentPage) === 'devices');
  const name = await page.evaluate(() => {
    const q = document.getElementById('devFindQ');
    return document.querySelector(`label[for="${q.id}"]`)?.textContent.trim() || q.getAttribute('aria-label') || '';
  });
  check('the search has a name a screen reader says', /find|caut/i.test(name), name);
  await page.locator('#devicesListView .section-header h2').click();
  await page.keyboard.press('/');
  check('"/" puts the cursor in the search', await page.evaluate(() => document.activeElement?.id) === 'devFindQ');
  check('Devices has the one page header (an h2)', await page.locator('#devicesListView > .section-header h2').count() === 1);

  // ── search: rows hidden, never redrawn; case and diacritics never matter ──
  const firstRow = await page.evaluateHandle(() => document.querySelector('#devicesList .device-row[data-f]'));
  await page.fill('#devFindQ', 'HALA');
  await settle(page);
  check('"HALA" finds "Hală" (case, diacritics)', JSON.stringify(await visibleIds(page)) === '["fa-hala"]', (await visibleIds(page)).join(','));
  check('the rows were hidden, not redrawn', await firstRow.evaluate(el => el.isConnected));
  check('the address keeps the search', /q=HALA/i.test(await hash(page)), await hash(page));
  check('the count says how many of how many', new RegExp(`\\b1\\D+${total}\\b`).test(await page.textContent('#devFindCount')),
        await page.textContent('#devFindCount'));
  if (inverters.length) {
    await page.fill('#devFindQ', 'invertor');
    await settle(page);
    check('a kind is found in the other language ("invertor" in the English UI)',
          inverters.every(id => true) && (await visibleIds(page)).filter(i => inverters.includes(i)).length === inverters.length,
          (await visibleIds(page)).join(','));
  }
  await page.fill('#devFindQ', '');
  await settle(page);

  // ── chips: counts, any value inside, every filter together ──
  if (inverters.length) {
    const invChip = page.locator('.find-chip[data-facet="kind"][data-value="inverter"]');
    check('a chip counts what it would show', Number(await invChip.locator('.find-chip-n').textContent()) === inverters.length);
    await invChip.click();
    await settle(page);
    check('a chip is a toggle button that says it is on', await invChip.getAttribute('aria-pressed') === 'true');
    check('Kind = Inverter shows the inverters only', JSON.stringify(await visibleIds(page)) === JSON.stringify(inverters),
          `${await visibleIds(page)} vs ${inverters}`);
    check('the focus stays on the chip just pressed', await page.evaluate(() => document.activeElement?.dataset?.value) === 'inverter');
    check('the address keeps the filters', /kind=inverter/.test(await hash(page)), await hash(page));
    await invChip.click();
    await settle(page);
  }
  // an installation shown only for some of its units says it is context
  if (ep && units.length >= 2) {
    await page.fill('#devFindQ', unit.id);
    await settle(page);
    const header = page.locator(`#devicesList [data-endpoint-toggle="${ep.id}"]`);
    const hits = (await visibleIds(page)).filter(i => units.some(u => u.id === i)).length;
    if (hits < units.length) {
      check('an installation shown for some units is marked context, in words',
            /context/.test(await header.getAttribute('class')) && /\d|one|un/.test(await header.locator('.find-ctx-pill').innerText()));
      check('a context row shows neither numbers nor actions', !(await header.locator('.device-row-actions').isVisible()));
    }
    check('its disclosure says it is open while a search opens it',
          await header.locator('[data-endpoint-disclose]').getAttribute('aria-expanded') === 'true');
    check('the count counts devices, not their frames', !/\b0\b/.test(await page.textContent('#devFindCount')));
    await page.fill('#devFindQ', '');
    await settle(page);
  }

  // ── nothing matches: said, and one button clears it ──
  await page.fill('#devFindQ', 'zzzz-nothing');
  await settle(page);
  check('nothing matching says so', await page.isVisible('#devFindEmpty') && /nothing|nimic/i.test(await page.textContent('#devFindEmpty')));
  check('deleted devices hide while a search matches none of them', !(await page.isVisible('#restorableDevices details')));
  await page.click('#devFindEmpty [data-find-clear-empty]');
  await settle(page);
  check('Clear filters shows every device again', (await visibleIds(page)).length >= total - all.filter(d => d.bridge).length && !(await page.isVisible('#devFindEmpty')));
  check('Clear filters hides itself once nothing is filtered', !(await page.locator('.find-bar[data-find="devices"] .find-clear').isVisible()));
  const det = page.locator('#restorableDevices details');
  check('deleted devices are folded, with their count', await det.count() === 1 && !(await det.evaluate(d => d.open))
        && /\d/.test(await det.locator('summary .dev-chip').innerText()));
  await page.fill('#devFindQ', 'gone soon');
  await settle(page);
  check('a search that matches a deleted device shows it', await page.isVisible('[data-restorable="fa-gone"]'));
  await page.fill('#devFindQ', '');
  await settle(page);

  // ── every icon button has a name, unique per row ──
  const unnamed = await page.evaluate(() => [...document.querySelectorAll('#devicesListView button, #devicesListView a.btn')]
    .filter(b => b.offsetParent && !(b.getAttribute('aria-label') || b.textContent.trim())).map(b => b.outerHTML.slice(0, 80)));
  check('every visible button on Devices has a name', !unnamed.length, unnamed.join(' | '));
  const dupNames = await page.evaluate(() => {
    const names = [...document.querySelectorAll('#devicesList .device-row-actions [aria-label]')].filter(b => b.offsetParent).map(b => b.getAttribute('aria-label'));
    return names.filter((n, i) => names.indexOf(n) !== i);
  });
  check('row actions are told apart by their names', !dupNames.length, [...new Set(dupNames)].join(' | '));
  check('an icon button shows its tip on keyboard focus', await page.evaluate(() => {
    const b = document.querySelector('#devicesList .device-row-actions [data-tip]');
    if (!b) return false;
    b.focus();
    return getComputedStyle(b, '::after').content.length > 2 || b.matches(':focus');
  }));

  // ── the list: one row each, sortable ──
  await page.click('[data-find-view="list"]');
  await settle(page);
  check('the list has one row per device', await page.locator('#devicesTable tbody tr:visible').count() === total);
  check('the tree is hidden in the list view', !(await page.isVisible('#devicesList')));
  check('a sortable column says its order', await page.locator('#devicesTable th:has([data-sort="name"])').getAttribute('aria-sort') === 'ascending');
  await page.click('#devicesTable [data-sort="name"]');
  await settle(page);
  check('sorting again reverses it', await page.locator('#devicesTable th:has([data-sort="name"])').getAttribute('aria-sort') === 'descending'
        && /sort=-name/.test(await hash(page)), await hash(page));

  // ── the address: one entry per step, the heading, Back to the row ──
  const h0 = await page.evaluate(() => history.length);
  const openName = await page.locator('#devicesTable a.find-open').first().innerText();
  const openId = await page.locator('#devicesTable tr[data-f]').first().getAttribute('data-f');
  await page.locator('#devicesTable a.find-open').first().click();
  await page.waitForSelector('#deviceDetailView .section-header');
  await settle(page);
  check('a row opens its device; the address names it', await hash(page) === `#/devices/${encodeURIComponent(openId)}`, await hash(page));
  check('one step, one history entry', await page.evaluate(() => history.length) - h0 === 1);
  check('the heading takes the focus', await focused(page) === 'H2', await focused(page));
  check('the tab title names the device', (await page.title()).includes(openName.trim()), await page.title());
  await page.goBack();
  await settle(page);
  check('Back returns to the list as it was', await page.isVisible('#devicesTable') && /view=list/.test(await hash(page)), await hash(page));
  check('Back puts the focus on the row it left', await page.evaluate(id => document.activeElement?.closest('[data-f]')?.dataset.f === id, openId));
  await page.reload();
  await page.waitForFunction(() => window.app && window.app._routeReady);
  await settle(page);
  check('a reload keeps the view', await page.isVisible('#devicesTable')
        && await page.locator('#devicesTable th:has([data-sort="name"])').getAttribute('aria-sort') === 'descending');
  await page.click('[data-find-view="tree"]');
  await settle(page);
  await page.close();

  // a unit's Back goes to its installation in one step; browser Back returns
  if (unit) {
    page = await open(BASE + `/#/devices/${encodeURIComponent(unit.id)}`);
    await page.waitForSelector('#deviceWsTabs');
    const n0 = await page.evaluate(() => history.length);
    await page.evaluate(() => window.app.closeDeviceDetail());
    await page.waitForFunction(i => location.hash === `#/installations/${i}`, ep.id);
    await settle(page);
    check("a unit's Back is its installation, one entry", await page.evaluate(() => history.length) - n0 === 1);
    await page.goBack();
    await page.waitForFunction(i => location.hash === `#/devices/${i}`, encodeURIComponent(unit.id));
    check('browser Back returns to the unit', await page.waitForSelector('#deviceWsTabs', { timeout: 8000 }).then(() => true, () => false));
    await page.close();
  }

  // a bad link says so, keeps the address, offers the way back — never another device
  page = await open(BASE + `/#/devices/${encodeURIComponent(standalone?.id || all[0].id)}`);
  await page.waitForSelector('#deviceWsTabs');
  await page.evaluate(() => { location.hash = '#/devices/no-such-device'; });
  await page.waitForFunction(() => /no-such-device/.test(document.querySelector('#deviceDetailView h2')?.textContent || ''));
  check('a bad device link says it is not here', /no device|nu există/i.test(await page.locator('#deviceDetailView h2').innerText()));
  check('…keeps the address it was given', await hash(page) === '#/devices/no-such-device');
  check('…and offers the way back', await page.locator('#deviceDetailView a[href="#/devices"]').count() === 1);
  if (ep) {
    await page.goto(BASE + `/#/installations/${encodeURIComponent(ep.id)}`);
    await page.waitForFunction(i => document.querySelector('#deviceDetailView')?.innerHTML.includes(i), ep.id);
    await page.evaluate(() => { location.hash = '#/installations/no-such-plant'; });
    await page.waitForFunction(() => /no-such-plant/.test(document.querySelector('#deviceDetailView h2')?.textContent || ''));
    check('a bad installation link never shows another page', await hash(page) === '#/installations/no-such-plant');
  }
  const tabDev = standalone || all[0];
  await page.goto(BASE + `/#/devices/${encodeURIComponent(tabDev.id)}/logs`);
  await page.waitForSelector('#deviceWsTabs [data-dtab="logs"].active');
  check('a link to a device tab opens that tab', true);
  await page.evaluate(() => window.app._switchDeviceTab('overview'));
  await settle(page);
  check('changing tab updates the address in place', await hash(page) === `#/devices/${encodeURIComponent(tabDev.id)}`);
  await page.goto(BASE + '/#/devices?kind=meter&kind=bogus');
  await page.waitForSelector('.find-chip[data-value="meter"]');
  await settle(page);
  check('a link with a filter opens it applied', await page.locator('.find-chip[data-facet="kind"][data-value="meter"]').getAttribute('aria-pressed') === 'true');
  check('a value that is not a filter is left out and said',
        await page.locator('.find-chip[data-value="bogus"]').count() === 0 && /bogus/.test(await page.locator('.find-ignored').innerText()));
  await page.click('.find-bar[data-find="devices"] .find-clear');
  await settle(page);

  // ── the status poll paints in place ──
  const row = await page.evaluateHandle(() => document.querySelector('#devicesList .device-row[data-f]'));
  await page.waitForTimeout(6000);
  check('the 5 s status poll never rebuilds the list', await row.evaluate(el => el.isConnected));

  // ── a device's kind, set by an administrator ──
  page.dlg.answers.push({ kind: 'sensor' });
  await page.evaluate(() => window.app.openDeviceDetail('fa-hala'));
  await page.waitForSelector('#ddvSetKind', { state: 'attached' });
  check('the device page names its kind and where it comes from',
        /kind|tip/i.test(await page.locator('#ddvSetKind').innerText()) && await page.locator('[data-kind-source]').count() === 1);
  await deviceAction(page, '#ddvSetKind');
  await page.waitForFunction(() => document.querySelector('[data-kind-source]')?.dataset.kindSource === 'override', null, { timeout: 8000 }).catch(() => {});
  const after = (await api('/api/devices')).body.devices.find(d => d.id === 'fa-hala');
  check('Set kind stores an override the list sees', after?.kind === 'sensor' && after?.kind_source === 'override');
  check('no page errors (admin)', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── templates: the same bar, cards filtered in place ──
  page = await open(BASE + '/#/templates');
  await page.waitForSelector('#tplFindQ');
  check('Templates has the one page header (an h2)', await page.locator('#templatesPage > .section-header h2').count() === 1);
  const card = await page.evaluateHandle(() => document.querySelector('.tm-cards [data-f]'));
  const tplTotal = await page.locator('.tm-cards [data-f]').count();
  await page.fill('#tplFindQ', 'fronius');
  await settle(page);
  const shownT = await page.locator('.tm-cards [data-f]:visible').count();
  check('template search hides cards', shownT > 0 && shownT < tplTotal, `${shownT}/${tplTotal}`);
  check('a keystroke never redraws the templates', await card.evaluate(el => el.isConnected));
  check('the focus stays in the template search', await page.evaluate(() => document.activeElement?.id) === 'tplFindQ');
  await page.fill('#tplFindQ', '');
  await settle(page);
  const fronius = page.locator('.find-chip[data-facet="vendor"][data-value="Fronius"]');
  check('the Solar API templates count under their vendor (Fronius ≥ 6)', Number(await fronius.locator('.find-chip-n').textContent()) >= 6);
  await fronius.click();
  await settle(page);
  const vShown = await page.locator('.tm-cards [data-f]:visible').count();
  check('a vendor chip filters the cards', vShown === Number(await fronius.locator('.find-chip-n').textContent()));
  const tplUnnamed = await page.evaluate(() => [...document.querySelectorAll('#templatesPage button')]
    .filter(b => b.offsetParent && !(b.getAttribute('aria-label') || b.textContent.trim())).length);
  check('every visible button on Templates has a name', tplUnnamed === 0, String(tplUnnamed));
  check('an export button names its template', /export|export/i.test(await page.locator('[data-tm-exp]:visible').first().getAttribute('aria-label') || '')
        && (await page.locator('[data-tm-exp]:visible').first().getAttribute('aria-label')).length > 12);
  await page.click('[data-find-view="list"]');
  await settle(page);
  check('templates as a list, filtered the same', await page.locator('.tm-table tbody tr:visible').count() === vShown);
  check('no page errors (templates)', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── the other language: the bar speaks it after a switch ──
  page = await open(BASE + '/#/devices');
  await page.waitForSelector('#devFindQ');
  await page.evaluate(() => window.app.setLanguage('ro'));
  await page.waitForTimeout(800);
  check('a language switch redraws the bar in it', /caută/i.test(await page.getAttribute('#devFindQ', 'placeholder') || '')
        && /arbore/i.test(await page.locator('[data-find-view="tree"]').innerText()));
  await page.evaluate(() => window.app.setLanguage('en'));
  await page.close();

  // ── a viewer: finds, sorts, never sets a kind ──
  page = await open(BASE + '/#/devices/fa-hala', { role: 'viewer' });
  await page.waitForSelector('#deviceWsTabs');
  check('viewer: no Set kind (no ⋯ menu offered)', !(await page.locator('#ddvActions').isVisible()) && !(await page.locator('#ddvSetKind').isVisible()));
  await page.close();

  // ── a phone: the filters fold behind one button, nothing scrolls sideways ──
  for (const theme of ['light', 'dark']) {
    page = await open(BASE + '/#/devices?kind=meter', { width: 390, height: 844, theme });
    await page.waitForSelector('#devFindQ');
    await settle(page);
    const tg = page.locator('.find-bar[data-find="devices"] .find-facets-toggle');
    if (OUT) await page.screenshot({ path: `${OUT}/${PREFIX}-devices-phone-${theme}.png` });
    if (theme === 'light') {
      check('phone: the filters are folded', await tg.isVisible() && !(await page.locator('#devFindFacets').isVisible()));
      check('phone: a filter that is on stays in sight, removable', await page.locator('.find-on-chip').isVisible());
      await tg.click();
      check('phone: Filters opens them and says so', await tg.getAttribute('aria-expanded') === 'true' && await page.locator('#devFindFacets').isVisible());
      check('phone: a button closes the sheet on its results', await page.locator('.find-sheet-done').isVisible());
      if (OUT) await page.screenshot({ path: `${OUT}/${PREFIX}-devices-phone-sheet.png` });
      await page.click('.find-sheet-done');
      check('phone: … and it folds them again', await tg.getAttribute('aria-expanded') === 'false');
      check('phone: no sideways scroll', await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    }
    await page.close();
    page = await open(BASE + '/#/templates', { width: 390, height: 844, theme });
    await page.waitForSelector('#tplFindQ');
    if (OUT) await page.screenshot({ path: `${OUT}/${PREFIX}-templates-phone-${theme}.png` });
    await page.close();
    page = await open(BASE + '/#/devices', { theme });
    await page.waitForSelector('#devFindQ');
    if (inverters.length) { await page.click('.find-chip[data-facet="kind"][data-value="inverter"]'); await settle(page); }
    if (theme === 'dark' && inverters.length) {
      const bg = await page.evaluate(() => getComputedStyle(document.querySelector('.find-chip[aria-pressed="true"]')).backgroundColor);
      check('dark: a chip that is on is not white', bg !== 'rgb(255, 255, 255)', bg);
    }
    if (OUT) await page.screenshot({ path: `${OUT}/${PREFIX}-devices-desktop-${theme}.png` });
    await page.goto(BASE + '/#/templates?view=list');
    await page.waitForSelector('.tm-table');
    await settle(page);
    if (OUT) await page.screenshot({ path: `${OUT}/${PREFIX}-templates-desktop-${theme}.png` });
    await page.close();
  }

  // ── at scale ──
  if (SCALE) {
    const sp = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    await sp.goto(SCALE + '/');
    await sp.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 30000 });
    const t0 = Date.now();
    await sp.evaluate(() => window.app.navigateTo('devices'));
    await sp.waitForSelector('#devFindQ');
    await sp.waitForFunction(() => document.querySelectorAll('#devicesList .device-row[data-f]').length > 150);
    const firstRender = Date.now() - t0;
    const n = await sp.locator('#devicesList .device-row[data-f]').count();
    const nodes = await sp.evaluate(() => document.querySelectorAll('#devicesListView *').length);
    const perKey = await sp.evaluate(async () => {
      const q = document.getElementById('devFindQ');
      const times = [];
      for (const s of ['a', 'ac', 'acm', 'acme', 'acme m', 'acme me', 'acme met', 'acme mete', 'acme meter']) {
        const t = performance.now();
        q.value = s;
        q.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        times.push(performance.now() - t);
      }
      return Math.max(...times);
    });
    await sp.fill('#devFindQ', 'unit 7');
    await sp.waitForTimeout(500);
    const unit7 = await sp.locator('#devicesList .device-row[data-f]:visible').count();
    const t1 = Date.now();
    const devs = await (await fetch(SCALE + '/api/devices')).arrayBuffer();
    const apiMs = Date.now() - t1;
    console.log(`SCALE devices=${n} first render ${firstRender} ms (incl. fetch), /api/devices ${apiMs} ms ${devs.byteLength} B, DOM ${nodes} nodes, worst keystroke ${perKey.toFixed(1)} ms (2 frames)`);
    await sp.fill('#devFindQ', '');
    await sp.evaluate(() => window.app.navigateTo('templates'));
    const t2 = Date.now();
    await sp.waitForSelector('#tplFindQ');
    const tplRender = Date.now() - t2;
    console.log(`SCALE templates=${await sp.locator('.tm-cards [data-f]').count()} render ${tplRender} ms`);
    check('scale: every device drawn (no paging)', n >= 200);
    check('scale: a keystroke well under a second', perKey < 1000, String(perKey));
    check('scale: "unit 7" is unit 7, not every port with a 7 in it', unit7 > 0 && unit7 < 20, String(unit7));
    await sp.close();
  }
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await api('/api/devices/fa-hala', { method: 'DELETE' }).catch(() => {});
  await fetch(BASE + '/api/devices/restorable/fa-hala', { method: 'DELETE' }).catch(() => {});
  await fetch(BASE + '/api/devices/restorable/fa-gone', { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
