// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Rules and templates as a user meets them (UI audit 2026-10-21, step 3 c):
// a rule that reads what the gateway does not have says so (preview, save,
// card, import); a rule whose command is no longer offered keeps its target;
// Add rule with nothing to command shows what to do; leaving armed says what
// was not returned to safe; the template View speaks the map's protocol and
// lists what else it holds; a canonical name fills Unit and Category (and
// declares it). Against the throwaway gateway of unchanged_save_e2e.mjs (its
// fixture). Every write that would change the fixture is answered by a route,
// and the one real save is put back.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 node tools/e2e/rules_templates_step3_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const condBefore = (await api('/api/rules/cond')).raw;

async function open() {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const toasts = page => page.locator('.toast').allInnerTexts().then(x => x.join(' | ')).catch(() => '');

try {
  let page = await open();
  await page.evaluate(() => window.app.navigateTo('rules'));
  await page.waitForSelector('[data-rule="cond"]');

  // ── preview and save of a rule that reads a device that is not here ──
  await page.locator('[data-rule="cond"] [data-action="openRuleModal"]').click();
  await page.waitForSelector('#ruleModal.active #rlWhen');
  await page.fill('#rlWhen', 'nosuch.x > 1');
  await page.click('#ruleModal [data-action="previewRule"]');
  await page.waitForTimeout(800);
  const preview = await page.locator('#rlPreview').innerText();
  check('preview: names the device that is not here', /nosuch\.x/.test(preview) && /no device or installation nosuch|nu există/i.test(preview), preview);
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(1200);
  const stillOpen = await page.locator('#ruleModal.active').count();
  check('save (shadow): saved, said, the editor stays open', stillOpen === 1 && /reads names|citește nume/i.test(await toasts(page)),
        `${stillOpen} ${await toasts(page)}`);
  check('save: the server kept the edit (a shadow rule may read it)', /nosuch/.test((await api('/api/rules/cond')).raw.when || ''));
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  await page.evaluate(() => window.app._loadRules());
  await page.waitForTimeout(600);
  const pill = page.locator('[data-rule="cond"] [data-rule-inputs]');
  check('card: an "inputs unknown" pill, amber in shadow', (await pill.count()) === 1 && /warn/.test(await pill.getAttribute('class')),
        String(await pill.count()));
  check('card: the problem in words under the header',
        /nosuch/.test(await page.locator('[data-rule="cond"] [data-rule-inputs-list]').innerText().catch(() => '')));
  // put the rule back as it was
  await fetch(BASE + '/api/rules/cond', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(condBefore) });

  // ── nothing offers the rule's command now: its target is kept ──
  await page.route('**/api/endpoints', r => r.fulfill({ json: { endpoints: [] } }));
  await page.route(/\/api\/devices(\?.*)?$/, async r => {
    const res = await r.fetch();
    const d = await res.json();
    for (const x of (d.devices || [])) x.commands = [];
    return r.fulfill({ json: d });
  });
  await page.evaluate(() => { window.app._devices = null; });
  let sent = null;
  await page.route('**/api/rules/cond', async r => {
    if (r.request().method() !== 'PUT') return r.continue();
    sent = r.request().postDataJSON();
    return r.fulfill({ json: { status: 'updated', rule: { ...sent, inputs: [] }, releasing: [] } });
  });
  await page.evaluate(() => window.app._loadRules());
  await page.waitForTimeout(500);
  await page.locator('[data-rule="cond"] [data-action="openRuleModal"]').click();
  await page.waitForSelector('#ruleModal.active #rlTarget');
  const sel = await page.evaluate(() => {
    const s = document.getElementById('rlTarget');
    const o = s.options[s.selectedIndex];
    return { value: s.value, notOffered: o && o.hasAttribute('data-not-offered'), cmd: document.getElementById('rlCommand').value };
  });
  check('not offered: its own target stays selected', sel.value === 'd:pv-u1' && sel.notOffered && sel.cmd === 'power_limit', JSON.stringify(sel));
  check('not offered: said above the form', await page.locator('#ruleModal [data-rule-not-offered]').isVisible());
  // Save with nothing touched: no change, no request
  sent = null;
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(500);
  check('untouched Save sends nothing', sent === null && !(await page.locator('#ruleModal.active').count()), JSON.stringify(sent));
  await page.locator('[data-rule="cond"] [data-action="openRuleModal"]').click();
  await page.waitForSelector('#ruleModal.active #rlTarget');
  await page.fill('#rlLabel', 'Condition with release · edited');
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(800);
  check('not offered: Save sends the same target', sent && sent.target?.device === 'pv-u1' && sent.target?.command === 'power_limit',
        JSON.stringify(sent?.target));
  await page.evaluate(() => window.app.closeModal('ruleModal'));

  // ── Add rule with nothing to command: what to do, no empty form ──
  await page.click('#rulesAddBtn');
  await page.waitForSelector('#ruleModal.active');
  await page.waitForTimeout(400);
  check('nothing to command: an empty state, not empty lists', await page.locator('#ruleModal [data-rule-empty]').isVisible()
        && !(await page.locator('#ruleModal #rlTarget').count()));
  check('nothing to command: no Save', !(await page.locator('#ruleModal [data-rule-save]').isVisible()));
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  await page.unroute('**/api/endpoints');
  await page.unroute(/\/api\/devices(\?.*)?$/);
  await page.unroute('**/api/rules/cond');
  await page.evaluate(() => { window.app._devices = null; });

  // ── a new rule saved with an unknown input: the editor stays, its id is set ──
  await page.route('**/api/rules', async r => {
    if (r.request().method() !== 'POST') return r.continue();
    const b = r.request().postDataJSON();
    return r.fulfill({ json: { status: 'created', rule: { ...b, inputs: [{ in: 'signal', ref: 'nosuch.x', problem: 'no_device' }] } } });
  });
  await page.click('#rulesAddBtn');
  await page.waitForSelector('#ruleModal.active #rlId');
  await page.fill('#rlId', 'e2e-new');
  await page.fill('#rlSignal', 'nosuch.x');
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(600);
  check('new rule with an unknown input: stays open, its id can no longer change',
        (await page.locator('#ruleModal.active').count()) === 1 && await page.locator('#rlId').isDisabled());
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  await page.unroute('**/api/rules');

  // ── the device is there, its command is not: kept, marked, and kept
  //    again after going to another target and back ──
  await page.route(/\/api\/devices(\?.*)?$/, async r => {
    const res = await r.fetch();
    const d = await res.json();
    for (const x of (d.devices || [])) x.commands = x.id === 'pv-u1' ? ['other_cmd'] : x.id === 'pv-u2' ? ['power_limit'] : [];
    return r.fulfill({ json: d });
  });
  await page.route('**/api/endpoints', r => r.fulfill({ json: { endpoints: [] } }));
  await page.evaluate(() => { window.app._devices = null; });
  await page.locator('[data-rule="cond"] [data-action="openRuleModal"]').click();
  await page.waitForSelector('#ruleModal.active #rlTarget');
  const kept = await page.evaluate(() => ({ t: document.getElementById('rlTarget').value, c: document.getElementById('rlCommand').value,
    marked: !!document.querySelector('#rlCommand option[data-not-offered]') }));
  check('command gone: its own command stays selected and marked', kept.t === 'd:pv-u1' && kept.c === 'power_limit' && kept.marked, JSON.stringify(kept));
  await page.selectOption('#rlTarget', 'd:pv-u2');
  await page.selectOption('#rlTarget', 'd:pv-u1');
  check('command gone: away and back keeps its own command', (await page.inputValue('#rlCommand')) === 'power_limit', await page.inputValue('#rlCommand'));
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  await page.unroute(/\/api\/devices(\?.*)?$/);
  await page.unroute('**/api/endpoints');
  await page.evaluate(() => { window.app._devices = null; });

  // ── an armed rule opened and saved untouched: no question, no request ──
  let armedPut = null;
  await page.route('**/api/rules/ov-u1', r => { if (r.request().method() === 'PUT') { armedPut = r.request().postDataJSON(); return r.fulfill({ json: { status: 'unchanged' } }); } return r.continue(); });
  const seenBefore = page.dlg.seen.length;
  await page.locator('[data-rule="ov-u1"] [data-action="openRuleModal"]').click();
  await page.waitForSelector('#ruleModal.active #rlTarget');
  await page.waitForTimeout(300);
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(500);
  check('armed rule saved untouched: no "Save an armed rule", no PUT', armedPut === null && page.dlg.seen.length === seenBefore,
        `${JSON.stringify(armedPut)} ${JSON.stringify(page.dlg.seen.slice(seenBefore))}`);
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  await page.unroute('**/api/rules/ov-u1');

  // ── leaving armed: what was not returned to safe is said ──
  await page.route('**/api/rules/ov-u1/mode', r => r.fulfill({ json: {
    status: 'ok', releasing: [], release_warning: 'its target pv-u1 is not here: nothing was sent back to safe' } }));
  await page.evaluate(() => window.app._loadRules());
  await page.waitForTimeout(500);
  page.dlg.answer = true;
  await page.locator('[data-rule="ov-u1"] [data-action="setRuleMode"]').click();
  await page.waitForTimeout(900);
  check('leaving armed: the release warning is a toast', /not returned to safe|nu a revenit/i.test(await toasts(page)) && /nothing was sent/.test(await toasts(page)),
        await toasts(page));
  await page.unroute('**/api/rules/ov-u1/mode');

  // ── import: what an imported rule reads that is not here ──
  await page.route('**/api/rules/import', r => r.fulfill({ json: { applied: false, ok: 1, total: 1, rules: [
    { id: 'from-elsewhere', label: 'From another site', status: 'new', errors: [],
      inputs: [{ in: 'signal', ref: 'meter9.voltage_l1', problem: 'no_device' }] }] } }));
  await page.evaluate(() => window.app.openRulesImport());
  await page.fill('#rulesImportText', 'rules: []');
  await page.evaluate(() => window.app.rulesImportPreview());
  await page.waitForTimeout(500);
  check('import: the report says what it reads that is not here',
        /meter9/.test(await page.locator('#rulesImportResult [data-rule-import-inputs]').innerText().catch(() => '')));
  await page.evaluate(() => window.app.closeModal('rulesImportModal'));
  await page.unroute('**/api/rules/import');

  // ── template View: the map's own protocol ──
  await page.evaluate(() => window.app.navigateTo('templates'));
  await page.waitForTimeout(500);
  await page.evaluate(() => window.app.viewTemplate('victron_gx_mqtt'));
  await page.waitForSelector('#tmViewModal.active');
  let body = await page.locator('#tmViewBody').innerText();
  check('View MQTT: MQTT · topics, not FC03', /MQTT · topic/.test(body) && !/FC03|FC04/.test(body), body.slice(0, 200));
  check('View MQTT: the first column is the topic', /topic/i.test(await page.locator('#tmViewBody [data-tpl-view-regs] th').first().innerText())
        && /N\/\{portal\}/.test(body));
  const also = await page.locator('#tmViewBody [data-tpl-view-also]').innerText().catch(() => '');
  check('View: calculated, commands, channels, components counted', /calculat/i.test(also) && /command|comenzi/i.test(also)
        && /channel|canale/i.test(also) && /component/i.test(also), also);
  await page.click('#tmViewBody [data-also="components"] summary');
  check('View: the components are listed', /battery/.test(await page.locator('#tmViewBody [data-also="components"]').innerText()));
  await page.evaluate(() => window.app.closeModal('tmViewModal'));
  await page.evaluate(() => window.app.viewTemplate('fronius_solar_api_inverter'));
  await page.waitForTimeout(500);
  body = await page.locator('#tmViewBody').innerText();
  check('View HTTP: HTTP · JSON and its JSON paths', /HTTP · JSON/.test(body) && /Body\.Data/.test(body) && !/FC03/.test(body), body.slice(0, 200));
  await page.evaluate(() => window.app.closeModal('tmViewModal'));
  await page.evaluate(() => window.app.viewTemplate('seplos_bms_v3_rtu_tap'));
  await page.waitForTimeout(500);
  body = await page.locator('#tmViewBody').innerText();
  check('View Modbus: a coil map says FC01', /FC01/.test(body), body.slice(0, 200));
  await page.evaluate(() => window.app.closeModal('tmViewModal'));

  // ── a canonical name fills Unit and Category, and declares the category ──
  await page.evaluate(() => window.app.openTplEditor());
  await page.waitForSelector('#devTplModal.active');
  await page.evaluate(() => window.app.tplAddRow());
  await page.waitForTimeout(300);
  const last = '#devTplBody tr[data-idx]:last-child';
  const setName = async v => {
    await page.locator(`${last} input[data-f="name"]`).fill(v);
    await page.locator(`${last} input[data-f="name"]`).dispatchEvent('change');
    await page.waitForTimeout(150);
  };
  await setName('voltage_l1_n');
  const filled = await page.evaluate(sel => ({ unit: document.querySelector(`${sel} input[data-f="unit"]`).value,
                                               cat: document.querySelector(`${sel} input[data-f="category"]`).value }), last);
  check('canonical: a new row takes the unit and the category', filled.unit === 'V' && filled.cat === 'voltage', JSON.stringify(filled));
  const declared = await page.evaluate(() => { window.app._tplCollectMeta(); return window.app._tplEdit.data.categories; });
  check('canonical: the category is declared, so Save is not refused', declared && declared.voltage && declared.voltage.label,
        JSON.stringify(declared));
  const checked = await page.evaluate(async () => (await (await fetch('/api/device-templates/check', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_template: { ...window.app._tplEdit.data, id: 'e2e_canon_check' } }) })).json()));
  check('canonical: the server finds no undeclared category', !JSON.stringify(checked).includes('not declared'), JSON.stringify(checked).slice(0, 300));
  await setName('my_reading');
  const back = await page.evaluate(sel => ({ unit: document.querySelector(`${sel} input[data-f="unit"]`).value,
                                             cat: document.querySelector(`${sel} input[data-f="category"]`).value }), last);
  check('canonical: a name that stops being canonical gives the section back', back.unit === '' && back.cat === 'basic', JSON.stringify(back));

  // the same fill on a calculated field (Advanced → Calculated)
  const calc = await page.evaluate(() => {
    const a = window.app;
    a._advAddCalc();
    const i = a._tplEdit.data.calculated.length - 1;
    a._advSet(['calculated', i, 'name'], 'str', { type: 'text', value: 'power_active_total', dataset: {} });
    return a._tplEdit.data.calculated[i];
  });
  check('canonical: a calculated field takes its unit too', calc.unit === 'W' && calc.category === 'power_active', JSON.stringify(calc));
  // a section the editor declared goes when nothing uses it any more
  const orphan = await page.evaluate(() => {
    const a = window.app, d = a._tplEdit.data;
    d.calculated.pop();
    d.registers.pop();
    a._tplCollectMeta();
    return Object.keys(d.categories);
  });
  check('canonical: a section the editor added goes with its last row', !orphan.includes('voltage') && !orphan.includes('power_active') && orphan.includes('basic'),
        orphan.join());
  await page.evaluate(() => window.app.closeModal('devTplModal'));

  // renaming a field that was already in the map never moves its data to
  // another InfluxDB measurement (the category)
  await page.evaluate(() => window.app.openTplEditor('seplos_bms_v3_rtu_tap', true));
  await page.waitForSelector('#devTplModal.active');
  await page.waitForTimeout(400);
  const renamed = await page.evaluate(() => {
    const a = window.app, d = a._tplEdit.data;
    const i = d.calculated.findIndex(c => c.name === 'power');
    a._advSet(['calculated', i, 'name'], 'str', { type: 'text', value: 'power_active_total', dataset: {} });
    const r0 = d.registers.find(r => !r.category) || null;
    a._tplCollectMeta();
    return { calc: d.calculated[i], cats: Object.keys(d.categories) };
  });
  check('canonical: renaming an existing calculated field keeps its category', renamed.calc.name === 'power_active_total'
        && !renamed.calc.category && !renamed.cats.includes('power_active'), JSON.stringify(renamed));
  const bulk = await page.evaluate(() => {
    const a = window.app, d = a._tplEdit.data;
    const r = d.registers[0];
    const before = r.category;
    r.name = 'voltage_l1_n';
    a._tplCanonFill(r);
    return { before, after: r.category };
  });
  check('canonical: an existing register renamed keeps its category', bulk.before === bulk.after, JSON.stringify(bulk));
  await page.evaluate(() => window.app.closeModal('devTplModal'));

  // counts read as counts: one channel, not "1 channels"
  await page.evaluate(() => window.app.viewTemplate('fronius_sunspec_inverter'));
  await page.waitForSelector('#tmViewModal.active');
  const chText = await page.locator('#tmViewBody [data-also="channels"] summary').innerText().catch(() => '');
  check('View: one channel reads singular', /^1 channel$/.test(chText.trim()), chText);
  await page.evaluate(() => window.app.closeModal('tmViewModal'));

  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  check('no native dialog', page.dlg.native === 0);
  await page.close();
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  // whatever happened, the fixture's rule is as it was
  await fetch(BASE + '/api/rules/cond', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(condBefore) }).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
