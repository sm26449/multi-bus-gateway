// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The shared pickers (ui/js/app-pickers.js) as a user meets them: a rule made
// with the keyboard alone — its target found by typing, its signal completed
// `device.` then `.field`, a misspelt field said with the nearest one; a
// calculated formula completing this device's own names; a template list that
// puts the kind the place is for first and folds the others, with vendor
// chips; the device wizard's template panel; a phone-width list that stays on
// the screen. The <select> under each picker still drives it.
//
// Against the safety fixture (tools/e2e/fixture_up.sh safety 18087): the one
// rule it saves (shadow) is deleted at the end.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 OUT=<dir> node tools/e2e/pickers_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => { const r = await fetch(BASE + path, opts); return { status: r.status, body: await r.json().catch(() => ({})) }; };
const RULE = 'e2e-pick';

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(opts = {}) {
  const page = await browser.newPage({ viewport: opts.viewport || { width: 1440, height: 950 }, colorScheme: opts.theme || 'light' });
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
const expanded = (page, sel) => page.getAttribute(sel, 'aria-expanded');
const activeText = page => page.evaluate(() => {
  const el = document.activeElement;
  const id = el && el.getAttribute('aria-activedescendant');
  return id ? (document.getElementById(id)?.textContent || '').replace(/\s+/g, ' ').trim() : '';
});
const optionTexts = (page, list) => page.locator(`${list} [role="option"]:visible`).allInnerTexts();

try {
  await api(`/api/rules/${RULE}`, { method: 'DELETE' });
  const page = await open();
  await page.evaluate(() => window.app.navigateTo('rules'));
  await page.waitForTimeout(600);
  await page.evaluate(() => window.app.openRuleModal(null));
  await page.waitForSelector('#ruleModal.active #rlTarget-pick');

  // ── the target: a combobox, found by typing ──
  const tgt = '#rlTarget-pick';
  check('target: a combobox named by its label', (await page.getAttribute(tgt, 'role')) === 'combobox'
        && await page.evaluate(() => document.querySelector('label[for="rlTarget-pick"]') !== null));
  check('target: the select under it is out of the tab order and hidden from readers',
        await page.evaluate(() => { const s = document.getElementById('rlTarget'); return s.tabIndex === -1 && s.getAttribute('aria-hidden') === 'true'; }));
  await page.focus(tgt);
  await page.keyboard.type('sunfield-u2');
  await page.waitForTimeout(200);
  check('target: typing opens the list, filtered', (await expanded(page, tgt)) === 'true'
        && (await optionTexts(page, '#ruleModal .cbx-pop:not([hidden])')).every(x => /u2/i.test(x)),
        (await optionTexts(page, '#ruleModal .cbx-pop:not([hidden])')).join(' | '));
  check('target: the active option is read out (aria-activedescendant)', /u2/i.test(await activeText(page)), await activeText(page));
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);
  check('target: Enter picks it — the select changes and the command list follows',
        (await page.evaluate(() => document.getElementById('rlTarget').value)) === 'd:sunfield-u2'
        && (await page.evaluate(() => document.getElementById('rlCommand').options.length)) > 0
        && (await expanded(page, tgt)) === 'false');
  // Escape closes the list, not the dialog
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Escape');
  check('target: Escape closes the list and keeps the dialog', (await expanded(page, tgt)) === 'false'
        && await page.locator('#ruleModal.active').count() === 1);
  // the select still drives it
  await page.selectOption('#rlTarget', 'd:sunfield-u1');
  check('target: the select set by code shows in the box, name and id', /unit 1 · sunfield-u1/i.test(await page.inputValue(tgt)), await page.inputValue(tgt));
  await page.evaluate(() => { document.getElementById('rlTarget').selectedIndex = 0; });
  check('target: selectedIndex set by code shows too', (await page.inputValue(tgt)) !== '' && !/unit 1 · /.test(await page.inputValue(tgt)), await page.inputValue(tgt));
  await page.selectOption('#rlTarget', 'd:sunfield-u1');
  // Home / End
  await page.focus(tgt);
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('End');
  const last = await activeText(page);
  await page.keyboard.press('Home');
  const first = await activeText(page);
  check('target: Home and End reach the first and the last', first && last && first !== last, `${first} / ${last}`);
  await page.keyboard.press('Escape');

  // ── a highlight left from an abandoned search is never picked later ──
  for (const how of ['Tab', 'Escape', 'click away']) {
    await page.selectOption('#rlTarget', 'g:sunfield/inverters');
    await page.click(tgt);
    await page.keyboard.type('u2');
    await page.waitForTimeout(100);
    if (how === 'click away') await page.click('#rlLabel');
    else if (how === 'Escape') { await page.keyboard.press('Escape'); await page.keyboard.press('Tab'); }
    else await page.keyboard.press('Tab');
    await page.waitForTimeout(100);
    await page.click(tgt);
    await page.waitForTimeout(100);
    const hl = await activeText(page);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(150);
    check(`target: left by ${how}, reopened, Enter — the target is unchanged`,
          (await page.evaluate(() => document.getElementById('rlTarget').value)) === 'g:sunfield/inverters' && !/u2/.test(hl), hl);
  }
  // the hidden select never keeps the focus, and a closed list never changes on an arrow
  await page.evaluate(() => document.getElementById('rlTarget').focus());
  check('target: focus aimed at the hidden select lands on the box', await page.evaluate(() => document.activeElement.id) === 'rlTarget-pick');
  await page.keyboard.press('ArrowDown');
  await page.waitForTimeout(100);
  check('target: ArrowDown on the closed box opens the list, the value stays',
        (await expanded(page, tgt)) === 'true' && (await page.evaluate(() => document.getElementById('rlTarget').value)) === 'g:sunfield/inverters');
  check('target: aria-selected marks the highlighted row', await page.evaluate(() => {
    const id = document.getElementById('rlTarget-pick').getAttribute('aria-activedescendant');
    const sel = [...document.querySelectorAll('#ruleModal .cbx-pop:not([hidden]) [role="option"][aria-selected="true"]')];
    return id && sel.length === 1 && sel[0].id === id;
  }));
  await page.keyboard.press('Escape');
  await page.selectOption('#rlTarget', 'd:sunfield-u1');

  // ── the signal: device, then field, completed as typed ──
  await page.fill('#rlId', RULE);
  await page.fill('#rlLabel', 'Picked with the keyboard');
  await page.focus('#rlSignal');
  await page.keyboard.type('sunfield-u');
  await page.waitForTimeout(400);
  const sugg = await optionTexts(page, '#ruleModal .cbx-formula .cbx-pop:not([hidden])');
  check('signal: typing a device offers the devices', sugg.some(x => /sunfield-u1/.test(x)) && sugg.some(x => /sunfield-u2/.test(x)), sugg.join(' | '));
  await page.keyboard.press('Enter');
  await page.waitForTimeout(500);
  const afterDev = await page.inputValue('#rlSignal');
  const fieldSugg = await optionTexts(page, '#ruleModal .cbx-formula .cbx-pop:not([hidden])');
  check('signal: a device chosen inserts "id." and offers its fields', /^sunfield-u\d\.$/.test(afterDev) && fieldSugg.length > 3, `${afterDev} · ${fieldSugg.length}`);
  check('signal: the fields carry their units before any reading', fieldSugg.some(x => /\b(A|V|W|Hz)\b/.test(x)), fieldSugg.slice(0, 5).join(' | '));
  await page.keyboard.type('current_dc');
  await page.waitForTimeout(300);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);
  const sig = await page.inputValue('#rlSignal');
  check('signal: a field chosen completes `device.field`', /^sunfield-u\d\.current_dc\w*$/.test(sig), sig);
  // ── an edit inside a name replaces the whole name ──
  const edit = async (value, caretAt, typed, keys = ['Enter']) => {
    await page.fill('#rlSignal', value);
    await page.evaluate(n => { const i = document.getElementById('rlSignal'); i.focus(); i.setSelectionRange(n, n); }, caretAt);
    if (typed) await page.keyboard.type(typed); else await page.keyboard.press('Control+Space');
    await page.waitForTimeout(400);
    for (const k of keys) { await page.keyboard.press(k); await page.waitForTimeout(250); }
    return page.inputValue('#rlSignal');
  };
  let v = 'sunfield-u1.current_l1 + 5';
  let r = await edit(v, v.indexOf('current_') + 'current_'.length, 'd');
  check('formula: a field edited in its middle is replaced whole', r === 'sunfield-u1.current_dc + 5', r);
  v = 'sunfield-u1.current_dc*2';
  r = await edit(v, 'sunf'.length, '', ['ArrowDown', 'ArrowDown', 'Enter']);
  if ((await expanded(page, '#rlSignal')) === 'true') await page.keyboard.press('Escape');
  r = await page.inputValue('#rlSignal');
  check('formula: a device picked inside a device name replaces it, the field stays', /^sunfield-(u2|meter-240|u7|u1)\.current_dc\*2$/.test(r) && !/sunfield-u\w*-/.test(r), r);
  v = 'max(sunfield-u1.curr';
  r = await edit(v, v.length, 'e');
  check('formula: at the end of the text', /^max\(sunfield-u1\.current_\w+$/.test(r), r);
  v = '1000-sunfield-u1.cur - 2';
  r = await edit(v, v.indexOf(' - 2'), 'r');
  check('formula: a number and a minus before a hyphenated id, an operator after', /^1000-sunfield-u1\.current_\w+ - 2$/.test(r), r);
  v = 'sunfield-u1.current_dc';
  r = await edit(v, 0, 'x', []);
  check('formula: a name typed at the start does not swallow the next one', r.startsWith('xsunfield-u1.current_dc') || r === 'xsunfield-u1.current_dc', r);
  if ((await expanded(page, '#rlSignal')) === 'true') await page.keyboard.press('Escape');

  // a misspelt field: said, with the nearest one, and one click fixes it
  await page.fill('#rlSignal', 'sunfield-u1.curent_dc');
  await page.waitForTimeout(900);
  const dym = await page.locator('#rlSignal-dym').innerText().catch(() => '');
  check('signal: a field the device lacks is said, with the nearest one', /curent_dc/.test(dym) && /current_dc/.test(dym), dym);
  await page.locator('#rlSignal-dym [data-fx-use]').first().click();
  check('signal: "Use" puts the nearest name in', (await page.inputValue('#rlSignal')) === 'sunfield-u1.current_dc', await page.inputValue('#rlSignal'));
  await page.fill('#rlSignal', 'sunfield-u1.current_dc + sunfield-u.current_dc');
  await page.waitForTimeout(900);
  const useDev = page.locator('#rlSignal-dym [data-fx-use][data-kind="device"]').first();
  if (await useDev.count()) await useDev.click();
  check('signal: "Use" fixes the misspelt device, not a longer id that starts the same',
        /^sunfield-u1\.current_dc \+ sunfield-u\w+\.current_dc$/.test(await page.inputValue('#rlSignal')) && (await page.inputValue('#rlSignal')).startsWith('sunfield-u1.current_dc + '),
        await page.inputValue('#rlSignal'));
  await page.fill('#rlSignal', 'nosuchdev.x');
  await page.waitForTimeout(900);
  check('signal: a device that is not here is said', /nosuchdev/.test(await page.locator('#rlSignal-dym').innerText().catch(() => '')));
  await page.fill('#rlSignal', 'sunfield-u1.current_dc');
  await page.fill('#rlSignalUnit', 'A');
  await page.fill('#rlRelease', '248');
  await page.fill('#rlParams', 'revert_s=0, ramp_s=10');
  if (OUT) {
    await page.focus('#rlSignal');
    await page.keyboard.press('Control+Space');
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${OUT}/pickers-signal-desktop-light.png` });
    await page.keyboard.press('Escape');
    await page.focus(tgt);
    await page.keyboard.press('ArrowDown');
    await page.waitForTimeout(200);
    await page.screenshot({ path: `${OUT}/pickers-target-desktop-light.png` });
    await page.keyboard.press('Escape');
  }
  // save (a new rule is saved in shadow)
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(1500);
  const saved = await api(`/api/rules/${RULE}`);
  if (saved.status !== 200) console.log('   feedback:', await page.locator('#ruleFeedback').innerText().catch(() => ''));
  check('the rule made with the pickers is saved as picked',
        saved.status === 200 && saved.body.raw?.target?.device === 'sunfield-u1' && saved.body.raw?.signal === 'sunfield-u1.current_dc',
        JSON.stringify(saved.body.raw || saved.body).slice(0, 300));
  await page.evaluate(() => window.app.closeModal('ruleModal'));

  // ── a calculated formula: this device's own names, at the caret ──
  await page.evaluate(() => window.app.openDeviceDetail('sunfield-meter-240'));
  await page.waitForTimeout(700);
  await page.evaluate(() => window.app._switchDeviceTab('calculated'));
  await page.waitForSelector('[data-action="calcAdd"]');
  await page.evaluate(() => window.app.calcAdd());
  await page.waitForSelector('#calcExpr');
  await page.waitForTimeout(400);
  await page.focus('#calcExpr');
  await page.keyboard.type('current_l');
  await page.waitForTimeout(500);
  const own = await optionTexts(page, '.cbx-formula .cbx-pop:not([hidden])');
  check('calculated: a bare name offers this device\'s own fields', own.some(x => /current_l1/.test(x)), own.join(' | '));
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);
  check('calculated: the field goes in at the caret', /^current_l\d$/.test(await page.inputValue('#calcExpr')), await page.inputValue('#calcExpr'));
  check('calculated: the preset list is a picker too', await page.locator('#calcPresetSel-pick[role="combobox"]').count() === 1);
  await page.evaluate(() => window.app.calcCancel());

  // ── a group's template: its kind first, the others folded, vendor chips ──
  await page.evaluate(() => window.app.openEndpointDetail('sunfield'));
  await page.waitForTimeout(900);
  await page.evaluate(() => window.app.openGroupModal('sunfield'));
  await page.waitForSelector('#endpointModal.active #grpTpl-pick');
  await page.selectOption('#grpRole', 'inverter');
  await page.focus('#grpTpl-pick');
  await page.keyboard.press('ArrowDown');
  await page.waitForTimeout(300);
  const pop = '#endpointModal .cbx-pop:not([hidden])';
  const groups = await page.locator(`${pop} .cbx-grp-label`).allInnerTexts();
  const opts = await optionTexts(page, pop);
  check('template: the kind the group holds comes first', /inverter/i.test(groups.filter(Boolean)[0] || ''), groups.join(' | '));
  check('template: the other kinds are folded under one line', opts.some(x => /other kinds/i.test(x)), opts.slice(-3).join(' | '));
  check('template: vendor chips filter the list', await page.locator(`${pop} .cbx-facet`).count() > 1);
  await page.keyboard.press('End');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);
  const unfolded = await optionTexts(page, pop);
  check('template: Enter on the folded line unfolds it', unfolded.length > opts.length && unfolded.some(x => /meter/i.test(x)), `${opts.length} → ${unfolded.length}`);
  await page.keyboard.type('eastron');
  await page.waitForTimeout(200);
  check('template: search reaches every kind', (await optionTexts(page, pop)).some(x => /eastron/i.test(x)));
  if (OUT) await page.screenshot({ path: `${OUT}/pickers-template-desktop-light.png` });
  await page.keyboard.press('Escape');
  // a refusal about the template lands on the box a person sees
  await page.fill('#grpId', 'e2e-pick');
  await page.fill('#grpUnits', '41');
  if (await page.locator('#grpAddr').count()) { await page.fill('#grpAddr', '127.0.0.1'); }
  const nGroups = (await api('/api/endpoints/sunfield')).body.groups.length;
  await page.route('**/api/endpoints/sunfield', r => r.request().method() === 'PUT'
    ? r.fulfill({ status: 422, json: { detail: { errors: [`groups[${nGroups}].template: no such template here`] } } })
    : r.continue());
  await page.click('#endpointModal [data-endpoint-save]');
  await page.waitForTimeout(600);
  check('template: a refusal about it focuses the visible box, marked invalid',
        await page.evaluate(() => document.activeElement.id) === 'grpTpl-pick'
        && (await page.getAttribute('#grpTpl-pick', 'aria-invalid')) === 'true',
        await page.evaluate(() => document.activeElement.id));
  const tplBefore = await page.evaluate(() => document.getElementById('grpTpl').value);
  await page.keyboard.press('ArrowDown');
  await page.waitForTimeout(100);
  check('template: an arrow then only opens the list', (await page.evaluate(() => document.getElementById('grpTpl').value)) === tplBefore
        && (await expanded(page, '#grpTpl-pick')) === 'true');
  await page.keyboard.press('Escape');
  await page.unroute('**/api/endpoints/sunfield');
  await page.evaluate(() => window.app.closeModal('endpointModal'));

  // ── the device wizard's template panel ──
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForTimeout(500);
  await page.evaluate(() => { window.app._devWiz.data.protocol = 'tcp'; window.app._devWiz.step = 2; window.app._devWizRender(); });
  await page.waitForSelector('#devWizTplList .tpl-pick');
  check('wizard: the template list is a listbox driven from the search box',
        (await page.getAttribute('#devWizTplSearch', 'role')) === 'combobox'
        && await page.locator('#devWizTplList [role="listbox"] .tpl-pick[role="option"]').count() > 3);
  await page.locator('[data-tplp-kind="inverter"]').click();
  await page.waitForTimeout(200);
  const wg = (await page.locator('#devWizTplList .cbx-grp-label').allInnerTexts()).filter(Boolean);
  check('wizard: a kind chip puts its templates first, the rest folded', /inverter/i.test(wg[0] || '')
        && (await optionTexts(page, '#devWizTplList')).some(x => /other kinds/i.test(x)), wg.join(' | '));
  await page.focus('#devWizTplSearch');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);
  const chosen = await page.evaluate(() => window.app._devWiz.data.template);
  check('wizard: Enter picks the active template, said in place', !!chosen
        && (await page.getAttribute(`#devWizTplList .tpl-pick[data-tpl="${chosen}"]`, 'aria-selected')) === 'true'
        && await page.locator('#devWizTplActions button').count() > 0, chosen);
  await page.locator('[data-tplp-kind="inverter"]').click();          // the chip off again: every kind
  await page.locator('.tpl-pick[data-tpl="eastron_sdm630"]').click().catch(() => {});
  check('wizard: a click on a template picks it (the old path)', (await page.evaluate(() => window.app._devWiz.data.template)) === 'eastron_sdm630');
  if (OUT) await page.screenshot({ path: `${OUT}/pickers-wizard-desktop-light.png` });
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));
  check('no native dialog', page.dlg.native === 0, String(page.dlg.native));
  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── 300 devices: opening and typing stay immediate ──
  {
    const p = await open();
    const real = (await api('/api/devices')).body.devices;
    const many = real.concat(Array.from({ length: 300 }, (_, i) => ({ ...real[0], id: `perf-${String(i).padStart(3, '0')}`,
      name: `Perf meter ${i}`, endpoint_id: '', component_of: undefined, protocol: 'tcp', kind: 'meter', enabled: i % 7 !== 0 })));
    await p.route(/\/api\/devices(\?.*)?$/, r => r.request().method() === 'GET' ? r.fulfill({ json: { devices: many } }) : r.continue());
    await p.evaluate(() => window.app.navigateTo('diagnostics'));
    await p.waitForSelector('#diagDeviceFilter-pick', { state: 'attached' });
    await p.waitForTimeout(600);
    const perf = await p.evaluate(async () => {
      const inp = document.getElementById('diagDeviceFilter-pick');
      const pop = inp.closest('.cbx').querySelector('.cbx-pop');
      inp.focus();
      let t0 = performance.now();
      inp.click();
      void pop.offsetHeight;
      const openMs = performance.now() - t0;
      const keys = [];
      for (const ch of 'perf 1') {
        t0 = performance.now();
        inp.value += ch;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        void pop.offsetHeight;
        keys.push(performance.now() - t0);
      }
      const shown = pop.querySelectorAll('[role="option"]:not([hidden])').length;
      return { openMs, avg: keys.reduce((a, b) => a + b, 0) / keys.length, max: Math.max(...keys), shown,
               total: pop.querySelectorAll('[role="option"]').length };
    });
    console.log(`   300 devices: open ${perf.openMs.toFixed(1)} ms, keystroke avg ${perf.avg.toFixed(1)} / max ${perf.max.toFixed(1)} ms, ${perf.shown}/${perf.total} shown`);
    check('300 devices: the list opens in under 50 ms', perf.openMs < 50, perf.openMs.toFixed(1));
    check('300 devices: a keystroke filters in under 16 ms', perf.avg < 16, `${perf.avg.toFixed(1)} ms`);
    // the dashboard switcher: a disabled device is listed, not opened
    await p.evaluate(() => window.app.navigateTo('dashboard'));
    await p.waitForTimeout(300);
    await p.evaluate(() => window.app.renderDashDeviceChips());
    await p.waitForTimeout(500);
    const dsw = await p.evaluate(() => {
      const off = document.querySelector('.dsw-row.dsw-off');
      return off ? { aria: off.getAttribute('aria-disabled'), id: off.dataset.id } : null;
    });
    if (dsw) {
      const before = await p.evaluate(() => window.app._dashDeviceId());
      await p.evaluate(id => { window.app._deviceSwitchOpen(true); document.querySelector(`.dsw-row[data-id="${id}"]`).click(); }, dsw.id);
      await p.waitForTimeout(300);
      check('switcher: a disabled device is marked and not opened', dsw.aria === 'true' && (await p.evaluate(() => window.app._dashDeviceId())) === before);
    } else check('switcher: a disabled device is marked and not opened', false, 'no disabled row');
    check('300 devices: no page errors', p.errs.length === 0, p.errs.join(' | '));
    await p.close();
  }

  // ── dark theme, phone width: the list stays on the screen ──
  for (const [theme, vp, tag] of [['dark', { width: 1440, height: 950 }, 'desktop-dark'], ['light', { width: 390, height: 844 }, 'phone-light'], ['dark', { width: 390, height: 844 }, 'phone-dark']]) {
    const p = await open({ theme, viewport: vp });
    await p.evaluate(() => window.app.navigateTo('rules'));
    await p.waitForTimeout(400);
    await p.evaluate(() => window.app.openRuleModal(null));
    await p.waitForSelector('#ruleModal.active #rlTarget-pick');
    await p.focus('#rlTarget-pick');
    await p.keyboard.press('ArrowDown');
    await p.waitForTimeout(300);
    const box = await p.locator('#ruleModal .cbx-pop:not([hidden])').boundingBox();
    const anchor = await p.locator('#rlTarget-pick').boundingBox();
    if (vp.height - (anchor.y + anchor.height) > box.height + 12) check(`${tag}: the list opens under the box when it fits`, box.y >= anchor.y + anchor.height - 1, `${box.y} vs ${anchor.y + anchor.height}`);
    check(`${tag}: the list is on the screen`, box && box.x >= 0 && box.x + box.width <= vp.width + 1 && box.y >= 0 && box.y + box.height <= vp.height + 1,
          JSON.stringify(box));
    const bg = await p.evaluate(() => getComputedStyle(document.querySelector('#ruleModal .cbx-pop:not([hidden])')).backgroundColor);
    if (theme === 'dark') check(`${tag}: the list is dark`, bg !== 'rgb(255, 255, 255)', bg);
    if (OUT) await p.screenshot({ path: `${OUT}/pickers-target-${tag}.png` });
    check(`${tag}: no page errors`, p.errs.length === 0, p.errs.join(' | '));
    await p.close();
  }
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await api(`/api/rules/${RULE}`, { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
