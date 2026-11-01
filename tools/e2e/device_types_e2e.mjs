// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Device types and identity (ADR-0027, lot L2), in a real browser:
// - Templates → Device types: built-ins read-only, a type of one's own added
//   with English and Romanian words, an icon and what it behaves like — from
//   the keyboard; a clash with a built-in refused in words;
// - the templates for a type (the filter), the template editor's kind and
//   "also suits these types";
// - a device given that type from its ⋯ menu: the Find facet shows it, the
//   type cannot be deleted while the device uses it (the device is named);
// - a device's identity on its Attributes tab: description, where it is,
//   labels, nameplate; a wrong label and a wrong date named by the server;
//   the label as a Find facet;
// - the same pages in Romanian.
//
// A throwaway gateway (127.0.0.1 only, nothing real reachable):
//   tools/e2e/fixture_up.sh types 18093
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18093 node tools/e2e/device_types_e2e.mjs
//   docker rm -f mbg-fx-types
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab, deviceAction, saveDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18093';
const DEV = 'em1';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  return { status: r.status, body };
};
const settle = page => page.waitForTimeout(450);

// from scratch: no type of ours, no identity
await api(`/api/devices/${DEV}/organise`, { method: 'PATCH', body: JSON.stringify({ type: null }) });
await api(`/api/attributes/devices/${DEV}/identity`, { method: 'DELETE' });
await api('/api/device-types/chiller', { method: 'DELETE' });
await api('/api/device-types/heat_recovery_unit', { method: 'DELETE' });
await api('/api/device-types/boiler_room', { method: 'DELETE' });

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(url, lang = 'en') {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  await ctx.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, lang);
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|status of (4\d\d|5\d\d)/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  await page.goto(url);
  await page.waitForLoadState('load');
  await page.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 20000 });
  await page.evaluate(() => { window.app._firstRunShown = true; window.app.closeModal?.('firstRunModal'); });
  return page;
}

async function typesTab(page) {
  await page.waitForSelector('#tmTabs [data-tmtab="types"]');
  await page.click('#tmTabs [data-tmtab="types"]');
  await page.waitForSelector('#tmTypesPanel table');
}

let page;
try {
  // ── the catalog: built-ins, then one of our own, from the keyboard ──
  page = await open(BASE + '/#/templates');
  await typesTab(page);
  check('the built-ins are listed', await page.locator('#tmTypesPanel [data-type-row="converter"]').count() === 1);
  check('a built-in cannot be edited or deleted',
        await page.locator('#tmTypesPanel [data-type-row="meter"] [data-action="typeDelete"]').count() === 0);
  check('the table has a name and column headers',
        !!(await page.getAttribute('#tmTypesPanel table', 'aria-label')) && await page.locator('#tmTypesPanel th[scope="col"]').count() === 5);

  page.dlg.answers.push({ id: 'chiller', en: 'Chiller', ro: 'Răcitor', icon: 'snow', behaves_like: 'heat_pump' });
  await page.focus('#typeAddBtn');
  await page.keyboard.press('Enter');
  await page.waitForSelector('#tmTypesPanel [data-type-row="chiller"]');
  const add = page.dlg.seen.at(-1);
  check('Add type asks id, words in both languages, icon, behaves like',
        JSON.stringify(add.fields.map(f => f.name)) === JSON.stringify(['id', 'en', 'ro', 'icon', 'behaves_like']),
        JSON.stringify(add.fields.map(f => f.name)));
  const row = page.locator('#tmTypesPanel [data-type-row="chiller"]');
  check('the new type shows its icon and words', await row.locator('i.bi-snow').count() === 1
        && /Chiller/.test(await row.innerText()) && /ro: Răcitor/.test(await row.innerText()));
  const saved = (await api('/api/device-types')).body.types.find(x => x.id === 'chiller');
  check('stored as given', saved && saved.behaves_like === 'heat_pump' && saved.labels.ro === 'Răcitor', JSON.stringify(saved));
  check('the focus lands on the new row', await page.evaluate(() => !!document.activeElement?.closest('[data-type-row="chiller"]')));

  // a clash with a built-in: refused, said in words, nothing stored
  page.dlg.answers.push({ id: 'system', en: 'My system', icon: 'other', behaves_like: 'other' });
  await page.click('#typeAddBtn');
  await settle(page);
  const refused = page.dlg.results.at(-1) || {};
  check('an id that is a built-in (an alias of site) is refused in words', refused.open && /built-in/.test((refused.errors || []).join(' ')),
        JSON.stringify(refused));
  check('nothing was stored', !(await api('/api/device-types')).body.types.some(x => x.id === 'system'));

  // the templates for the type: its kind's (heat pump)
  const tplBtn = row.locator('[data-action="typeTemplatesToggle"]');
  check('the template count is a button that says it is closed', await tplBtn.getAttribute('aria-expanded') === 'false');
  await tplBtn.focus();
  await page.keyboard.press('Enter');
  await page.waitForSelector('#typeTpl_chiller:not([hidden]) li');
  check('…and opens the templates for a chiller (the heat-pump maps)',
        await tplBtn.getAttribute('aria-expanded') === 'true' && /Samsung/i.test(await page.locator('#typeTpl_chiller').innerText()),
        await page.locator('#typeTpl_chiller').innerText());
  const api_t = (await api('/api/device-templates?type=chiller')).body.templates.map(x => x.id);
  check('the API filter says the same', api_t.includes('samsung_mim_b19n') && !api_t.includes('eastron_sdm630'), api_t.join(','));

  // ── the template editor: kind and "also suits" ──
  await page.evaluate(() => window.app.openTplEditor('eastron_sdm120', true));
  await page.waitForSelector('#tplKind');
  check('the editor shows the map\'s kind', await page.inputValue('#tplKind') === 'meter');
  check('the kind has a label and a hint', await page.locator('label[for="tplKind"]').count() === 1
        && !!(await page.getAttribute('#tplKind', 'aria-describedby')));
  const chk = page.locator('[data-tpl-type="chiller"]');
  check('…and offers our type under "also suits"', await chk.count() === 1);
  await page.fill('#tplId', 'e2e_chiller_meter');
  await chk.check();
  await page.evaluate(() => window.app.tplSave());
  await settle(page);
  const got = (await api('/api/device-templates/e2e_chiller_meter')).body?.device_template || {};
  check('saved with its kind and the type it suits', got.kind === 'meter' && JSON.stringify(got.types) === '["chiller"]', JSON.stringify({ k: got.kind, t: got.types }));
  const api_t2 = (await api('/api/device-templates?type=chiller')).body.templates.map(x => x.id);
  check('the type now lists that map too', api_t2.includes('e2e_chiller_meter'), api_t2.join(','));
  await page.close();

  // ── a device of that type ──
  page = await open(BASE + `/#/devices/${DEV}`);
  await page.waitForSelector('#ddvActions');
  check('the device menu says Type', /^\s*Type:/.test(await page.locator('#ddvSetKind').textContent()));
  await deviceAction(page, '#ddvSetKind');
  await page.waitForSelector('#devEditModal.active #devEditType');
  check('the type dialog is a labelled dialog',
        await page.getAttribute('#devEditModal .modal-content', 'role') === 'dialog'
        && !!(await page.getAttribute('#devEditModal .modal-content', 'aria-labelledby')));
  check('…with the focus on the type', await page.evaluate(() => document.activeElement?.id) === 'devEditType');
  const opts = await page.locator('#devEditType option').evaluateAll(os => os.map(o => o.value));
  check('the type picker offers our type and the built-ins', opts.includes('chiller') && opts.includes('converter'), opts.join(','));
  // "+ Add type…" in place, from the keyboard
  const addBtn = page.locator('#deTypeAddBtn');
  check('"+ Add type…" says it is closed and what it opens',
        await addBtn.getAttribute('aria-expanded') === 'false' && await addBtn.getAttribute('aria-controls') === 'deTypeForm');
  await addBtn.focus();
  await page.keyboard.press('Enter');
  await page.waitForSelector('#deTypeForm:not([hidden])');
  check('…opens the form with the focus on the English name',
        await addBtn.getAttribute('aria-expanded') === 'true' && await page.evaluate(() => document.activeElement?.id) === 'deTnEn');
  await page.keyboard.type('Heat recovery unit');
  check('the id is made from the English name', await page.inputValue('#deTnId') === 'heat_recovery_unit', await page.inputValue('#deTnId'));
  await page.fill('#deTnRo', 'Recuperator de căldură');
  await page.selectOption('#deTnIcon', 'fan');
  await page.selectOption('#deTnLike', 'heat_pump');
  // a refused id is said in words, at its field
  await page.fill('#deTnId', 'meter');
  await page.click('#deTnCreate');
  await page.waitForSelector('#deTnErr:not([hidden])');
  check('a built-in id is refused at its field, in words', /built-in/.test(await page.textContent('#deTnErr'))
        && await page.getAttribute('#deTnId', 'aria-invalid') === 'true');
  await page.fill('#deTnId', 'heat_recovery_unit');
  await page.locator('#deTnId').press('Enter');
  await page.waitForFunction(() => document.getElementById('devEditType')?.value === 'heat_recovery_unit');
  check('created and picked without leaving the page', await page.isVisible('#devEditModal.active')
        && await page.locator('#deTypeForm').isHidden());
  const hr = (await api('/api/device-types')).body.types.find(x => x.id === 'heat_recovery_unit');
  check('…stored with its words, icon and behaviour', hr && hr.labels.ro === 'Recuperator de căldură' && hr.icon === 'fan'
        && hr.behaves_like === 'heat_pump', JSON.stringify(hr));
  // the one wanted for this device is the chiller
  await page.selectOption('#devEditType', 'chiller');
  await saveDeviceEditor(page);                       // the Edit modal's one Save (L3.4)
  await settle(page);
  const d = (await api('/api/devices')).body.devices.find(x => x.id === DEV);
  check('the device is a chiller; on the wire it behaves like a heat pump',
        d.type === 'chiller' && d.kind === 'heat_pump' && d.type_label === 'Chiller', JSON.stringify({ t: d.type, k: d.kind }));

  // ── meter-only: the same "+ Add type…" on Add Device's "what is it?" ──
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
  await page.click('[data-ad-way="measured"]');                 // It doesn't communicate
  await page.waitForSelector('#adTypeAddBtn');
  check('meter-only: our non-meter types are offered', await page.locator('input[name="adKind"][value="chiller"]').count() === 1);
  await page.click('#adTypeAddBtn');
  await page.fill('#adTnEn', 'Boiler room');
  check('meter-only: a meter cannot be picked to behave like', await page.locator('#adTnLike option[value="meter"]').count() === 0);
  await page.click('#adTnCreate');
  await page.waitForSelector('input[name="adKind"][value="boiler_room"]:checked', { state: 'attached' });
  await page.waitForTimeout(150);
  const mo = await page.evaluate(() => [window.app._ad.s.kind, document.activeElement?.value, document.activeElement?.tagName]);
  check('meter-only: the new type is created and picked',
        await page.evaluate(() => window.app._ad.s.kind) === 'boiler_room'
        && mo[1] === 'boiler_room', JSON.stringify(mo));
  await page.evaluate(() => { window.app._ad.done = true; window.app.closeModal('addDeviceModal'); });

  // ── identity on the Attributes tab ──
  await openDeviceTab(page, 'attributes');
  await page.waitForSelector('#deviceDetailView .attr-add-sel');
  check('identity is offered', await page.locator('#deviceDetailView .attr-add-sel option[value="identity"]').count() === 1);
  await page.selectOption('#deviceDetailView .attr-add-sel', 'identity');
  await page.click('#deviceDetailView [data-action="attrAdd"]');
  const f = n => `#deviceDetailView .attr-editor-host [data-attr-f="${n}"]`;
  await page.waitForSelector(f('description'));
  check('the description is a text area with its limit said',
        await page.evaluate(s => document.querySelector(s).tagName, f('description')) === 'TEXTAREA'
        && !!(await page.getAttribute(f('description'), 'aria-describedby')));
  check('every field has a label', await page.evaluate(() =>
    [...document.querySelectorAll('#deviceDetailView .attr-editor-host [data-attr-f]')]
      .every(el => document.querySelector(`label[for="${el.id}"]`))));
  check('the date is a date field', await page.getAttribute(f('installed_on'), 'type') === 'date');
  await page.fill(f('description'), 'Main meter of the cabinet.\nRead every second.');
  await page.fill(f('location'), 'garage, cabinet 2');
  await page.fill(f('labels'), 'HVAC, floor-1');
  await page.fill(f('manufacturer'), 'Eastron');
  await page.fill(f('model'), 'SDM630');
  await page.fill(f('serial_number'), 'SN-0042');
  await page.click('#deviceDetailView [data-action="attrSave"]');
  await page.waitForFunction(() => (document.querySelector('#deviceDetailView .attr-err')?.textContent || '').length > 0);
  const err = await page.textContent('#deviceDetailView .attr-err');
  check('a label in capitals is refused in words', /HVAC/.test(err) && /label/.test(err), err);
  check('the error is announced', await page.getAttribute('#deviceDetailView .attr-err', 'role') === 'alert');
  await page.fill(f('labels'), 'hvac, floor-1');
  await page.fill(f('installed_on'), '2024-05-17');
  await page.click('#deviceDetailView [data-action="attrSave"]');
  await page.waitForSelector('#deviceDetailView .attr-cats [data-attr-cat="identity"]');
  const held = (await api(`/api/attributes/devices/${DEV}`)).body.attributes.identity;
  check('identity saved', held?.labels?.join(',') === 'hvac,floor-1' && held.installed_on === '2024-05-17'
        && held.description.includes('\n'), JSON.stringify(held));
  check('the labels are shown as chips', await page.locator('#deviceDetailView [data-attr-cat="identity"] .dev-chip').count() === 2);
  const bad = await api(`/api/attributes/devices/${DEV}/identity`, { method: 'PUT', body: JSON.stringify({ installed_on: '2024-02-30' }) });
  check('an impossible date is refused by the server', bad.status === 422 && /YYYY-MM-DD/.test(JSON.stringify(bad.body)));
  await page.close();

  // ── Find: the type and the label as facets ──
  page = await open(BASE + '/#/devices');
  await page.waitForSelector('#devFindQ');
  await page.click('.find-bar[data-find="devices"] .find-facets-toggle').catch(() => {});
  const typeChip = page.locator('.find-chip[data-facet="kind"][data-value="chiller"]');
  await typeChip.waitFor();
  check('Find has our type as a filter, with its icon', /Chiller/.test(await typeChip.innerText()) && await typeChip.locator('i.bi-snow').count() === 1);
  await typeChip.focus();
  await page.keyboard.press('Enter');
  await settle(page);
  const vis = await page.locator('#devicesList [data-f]:visible').evaluateAll(els => [...new Set(els.map(e => e.dataset.f))]);
  check('…which shows only the chiller', JSON.stringify(vis) === JSON.stringify([DEV]), vis.join(','));
  check('…and says it is on', await typeChip.getAttribute('aria-pressed') === 'true');
  await typeChip.click();
  await settle(page);
  const labelChip = page.locator('.find-chip[data-facet="label"][data-value="hvac"]');
  await labelChip.waitFor();
  await labelChip.click();
  await settle(page);
  const vis2 = await page.locator('#devicesList [data-f]:visible').evaluateAll(els => [...new Set(els.map(e => e.dataset.f))]);
  check('a label filters the devices', JSON.stringify(vis2) === JSON.stringify([DEV]), vis2.join(','));
  check('the label filter is in the address', /label=hvac/.test(await page.evaluate(() => location.hash)));
  await page.close();

  // ── a type in use is not deleted; the device is named ──
  page = await open(BASE + '/#/templates');
  await typesTab(page);
  await page.click('#tmTypesPanel [data-type-row="chiller"] [data-action="typeDelete"]');
  await settle(page);
  const del = page.dlg.results.at(-1) || {};
  check('a type a device uses is not deleted, and the device is named', del.open && /em1/.test((del.errors || []).join(' ')) && /e2e_chiller_meter/.test((del.errors || []).join(' ')),
        JSON.stringify(del));
  check('…it is still there', (await api('/api/device-types')).body.types.some(x => x.id === 'chiller'));
  check('the delete button names the type for a screen reader',
        /Chiller/.test(await page.getAttribute('#tmTypesPanel [data-type-row="chiller"] [data-action="typeDelete"]', 'aria-label')));
  await page.close();

  // ── Romanian ──
  page = await open(BASE + '/#/templates', 'ro');
  await page.waitForSelector('#tmTabs [data-tmtab="types"]');
  check('ro: the tab is "Tipuri de dispozitive"', /Tipuri de dispozitive/.test(await page.textContent('#tmTabs [data-tmtab="types"]')));
  await typesTab(page);
  check('ro: our type in its Romanian words', /Răcitor/.test(await page.locator('#tmTypesPanel [data-type-row="chiller"]').innerText()));
  check('ro: a built-in in Romanian', /Convertor/.test(await page.locator('#tmTypesPanel [data-type-row="converter"]').innerText()));
  check('ro: template counts in Romanian plural', /șabloane|șablon/.test(await page.locator('#tmTypesPanel [data-type-row="meter"] [data-action="typeTemplatesToggle"]').innerText()));
  await page.close();
  page = await open(BASE + `/#/devices/${DEV}`, 'ro');
  await page.waitForSelector('#ddvActions');
  check('ro: the device menu says Tip', /^\s*Tip:/.test(await page.locator('#ddvSetKind').textContent()));
  await deviceAction(page, '#ddvSetKind');
  await page.waitForSelector('#devEditModal.active #deTypeAddBtn');
  check('ro: "Adaugă tip…" in the type dialog', /Adaugă tip/.test(await page.textContent('#deTypeAddBtn')));
  await page.keyboard.press('Escape');
  await page.close();
  page = await open(BASE + '/#/devices', 'ro');
  await page.waitForSelector('#devFindQ');
  await page.click('.find-bar[data-find="devices"] .find-facets-toggle').catch(() => {});
  const roChip = page.locator('.find-chip[data-facet="kind"][data-value="chiller"]');
  await roChip.waitFor();
  check('ro: the Find filter says Răcitor', /Răcitor/.test(await roChip.innerText()));
  check('ro: the label filter is named "Etichetă"', /Etichetă/.test(await page.locator('.find-bar[data-find="devices"] .find-facets').textContent()));
  await page.fill('#devFindQ', 'racitor');
  await settle(page);
  const vis3 = await page.locator('#devicesList [data-f]:visible').evaluateAll(els => [...new Set(els.map(e => e.dataset.f))]);
  check('ro: "racitor" (no diacritics) finds the chiller', JSON.stringify(vis3) === JSON.stringify([DEV]), vis3.join(','));
  check('no error on any page', !page.errs.length, page.errs.join(' | '));
  await page.close();
} catch (e) {
  check('the run finished', false, String(e && e.stack || e));
} finally {
  // leave the gateway as it was
  await api(`/api/devices/${DEV}/organise`, { method: 'PATCH', body: JSON.stringify({ type: null }) });
  await api(`/api/attributes/devices/${DEV}/identity`, { method: 'DELETE' });
  await api('/api/device-templates/e2e_chiller_meter', { method: 'DELETE' });
  await api('/api/device-types/chiller', { method: 'DELETE' });
  await api('/api/device-types/heat_recovery_unit', { method: 'DELETE' });
  await api('/api/device-types/boiler_room', { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
