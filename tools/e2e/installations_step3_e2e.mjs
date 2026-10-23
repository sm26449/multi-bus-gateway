// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The installation pages as a user meets them (UI audit 2026-10-21, section
// C, lot b): Add group offers the gateway's own ways of reading and says a
// wrong address at its field; editing a group keeps default unit names
// unwritten; "Publishes to" follows a new group; the wizard keeps step 2
// across Back, asks the datalogger again for a new address, keeps the Smart
// Meter on its Modbus unit and says a group it cannot read instead of
// dropping it, and offers templates by their declared kind; a virtual meter
// is added with the fields Edit has, and its switch shows what the meter is;
// a unit's Calculated tab says a value is that unit's alone; the PQ tab never
// shows another device's counters.
//
// Against a throwaway gateway on the installation_safety fixture (`safety`
// variant, writes on) plus a disabled Janitza `pq1` at 127.0.0.1:15503:
// every address is the container's loopback, nothing real is reached. It
// adds and removes a group `extra`, a virtual meter template `e2e_vm` and its
// instance; it puts the fixture back in `finally`.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18095 OUT=<dir> node tools/e2e/installations_step3_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18095';
const OUT = process.env.OUT || '';
const EP = 'sunfield';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open() {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.puts = [];
  page.saved = [];
  page.on('request', r => { if (r.method() === 'PUT' && /\/api\/endpoints\//.test(r.url())) page.puts.push(r.postDataJSON()); });
  page.on('response', async r => {
    if (r.request().method() === 'PUT' && /\/api\/endpoints\//.test(r.url())) page.saved.push(await r.json().catch(() => ({})));
  });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  await page.waitForFunction(() => window.app && window.app.navigateTo);
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const openEndpoint = async page => {
  await page.evaluate(id => window.app.openEndpointDetail(id), EP);
  await page.waitForSelector('[data-endpoint-page]');
};
const modalSave = page => page.click('#endpointModal [data-endpoint-save]');

// the group `extra` goes again through the page's own save (its stored shape)
const removeExtra = page => page.evaluate(async ep => {
  const p = window.app._endpointDetail;
  if (!p || p.id !== ep || !(p.groups || []).some(g => g.id === 'extra')) return;
  await window.app._saveGroups(ep, p.groups.filter(g => g.id !== 'extra').map(g => window.app._rawGroup(p, g.id)));
}, EP);

// each part on its own page: one that breaks says so, the next still runs
const pageErrs = [];
let natives = 0;
async function section(name, fn) {
  const page = await open();
  try { await fn(page); }
  catch (e) { check(`${name}: ran to the end`, false, String(e).split('\n')[0]); }
  finally {
    pageErrs.push(...page.errs);
    natives += page.dlg.native;
    await removeExtra(page).catch(() => {});
    await page.close();
  }
}
const toEndpoint = async page => {
  await page.evaluate(() => window.app.navigateTo('devices'));
  await openEndpoint(page);
};

try {
  // ── P5: Add group offers the gateway's ways; an address is judged at its field ──
  await section('Add group', async page => {
  await toEndpoint(page);
  await page.evaluate(id => window.app.openGroupModal(id, ''), EP);
  await page.waitForSelector('#grpProto');
  const protos = await page.$$eval('#grpProto option', os => os.map(o => o.value));
  const lists = await api('/api/protocols');
  const want = lists.group.filter(x => !lists.serial.includes(x));
  check('Add group: the ways of reading are the API\'s, serial ones apart',
        JSON.stringify(protos) === JSON.stringify(want), `${protos} vs ${want}`);
  await page.selectOption('#grpProto', 'http');
  check('Add group: HTTP asks for a URL, empty, without a port',
        (await page.inputValue('#grpAddr')) === '' && /URL/.test(await page.innerText('#grpAddrLabel'))
        && !(await page.isVisible('#grpPort')));
  await page.fill('#grpId', 'extra');
  await page.fill('#grpUnits', '30');
  await page.fill('#grpAddr', '192.168.1.5');
  page.puts.length = 0;
  await modalSave(page);
  await page.waitForTimeout(500);
  const addrErr = await page.locator('#grpAddr-err').innerText().catch(() => '');
  check('Add group: a host given as a URL is said at the field, nothing sent',
        /http/i.test(addrErr) && !page.puts.length, `${addrErr} · ${page.puts.length} PUT`);
  await page.selectOption('#grpProto', 'tcp');
  await page.fill('#grpAddr', '127.0.0.1');
  await page.selectOption('#grpTpl', '');
  await modalSave(page);
  await page.waitForTimeout(500);
  const tplErr = await page.locator('#grpTpl-err').innerText().catch(() => '');
  check('Add group: no template on the group nor the installation is said at the field',
        /template/i.test(tplErr) && !page.puts.length, `${tplErr} · ${page.puts.length} PUT`);
  });

  // ── P18: the new group shows in "Publishes to" without reopening ──
  await section('Publishes to', async page => {
  await toEndpoint(page);
  const pubBefore = (await page.locator('#plPublish').innerText()).split('·').length;
  await page.evaluate(id => window.app.openGroupModal(id, ''), EP);
  await page.waitForSelector('#grpId');
  await page.fill('#grpId', 'extra');
  await page.fill('#grpUnits', '30');
  await page.selectOption('#grpRole', 'meter');
  await page.waitForTimeout(150);
  await page.selectOption('#grpTpl', 'fronius_sunspec_meter');
  await page.fill('#grpAddr', '127.0.0.1');
  await page.fill('#grpPort', '9');
  await modalSave(page);
  let created = false;                         // a new group starts its units: give it time
  for (let i = 0; i < 20 && !created; i++) {
    await page.waitForTimeout(500);
    created = (await api(`/api/endpoints/${EP}`)).groups.some(g => g.id === 'extra');
  }
  check('Add group: a right one is created', created,
        `${await page.locator('#endpointFeedback').innerText().catch(() => '')} · ${await page.locator('#endpointModal .field-error').allInnerTexts().catch(() => [])}`);
  await page.waitForTimeout(5500);           // one tick of the page's refresh
  const pub = await page.locator('#plPublish').innerText();
  check('"Publishes to" lists the new group without reopening the page', pub.split('·').length === pubBefore + 1, pub);
  });

  // ── P11: editing a group writes no default unit name ──
  await section('Edit group', async page => {
  await toEndpoint(page);
  page.puts.length = 0;
  await page.evaluate(id => window.app.openGroupModal(id, 'inverters'), EP);
  await page.waitForSelector('#grpUnits');
  const cmd = page.locator('#endpointModal [data-grp-cmd]').first();
  await cmd.uncheck(); await cmd.check();
  await modalSave(page);
  await page.waitForTimeout(1200);
  const sent = (page.puts[0]?.groups || []).find(g => g.id === 'inverters');
  check('Edit group: default units go back as bare numbers',
        sent && JSON.stringify(sent.units) === '[1,2]', JSON.stringify(sent?.units));
  });

  // ── a group that takes the installation's template keeps taking it ──
  // (production: the battery bank's group inherits its tap template)
  await fetch(`${BASE}/api/endpoints`, json('POST', {
    id: 'e2ebank', name: 'E2E bank', enabled: false, template: 'fronius_sunspec_meter',
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 15504 },
    groups: [{ id: 'units', role: 'meter', units: [1, 2] }] }));
  await section('Inherited template', async page => {
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.evaluate(() => window.app.openEndpointDetail('e2ebank'));
  await page.waitForSelector('[data-endpoint-page]');
  const own = async () => (await api('/api/endpoints/e2ebank')).groups.map(g => g.template_declared);
  // an installation made by POST is written in full by its first save (the
  // defaults made explicit): the second one is the "nothing changed" test
  const editSave = async () => {
    await page.evaluate(() => window.app.openGroupModal('e2ebank', 'units'));
    await page.waitForSelector('#grpUnits');
    await modalSave(page);
    await page.waitForTimeout(1200);
  };
  await page.evaluate(() => window.app.openGroupModal('e2ebank', 'units'));
  await page.waitForSelector('#grpUnits');
  check('Edit group: an inherited template is shown as the installation\'s',
        /installation/.test(await page.$eval('#grpTpl option[value=""]', o => o.textContent))
        && (await page.inputValue('#grpTpl')) === '');
  await page.evaluate(() => window.app.closeModal('endpointModal'));
  await editSave();
  page.saved.length = 0;
  await editSave();
  check('Edit group: saved again, unchanged; the group still takes the installation\'s template',
        page.saved[0]?.status === 'unchanged' && JSON.stringify(await own()) === '[""]',
        `${JSON.stringify(page.saved.map(x => x.status))} · ${JSON.stringify(await own())}`);
  await page.evaluate(() => window.app.openGroupModal('e2ebank', ''));
  await page.waitForSelector('#grpId');
  await page.fill('#grpId', 'more');
  await page.fill('#grpUnits', '3');
  await page.selectOption('#grpRole', 'meter');
  await page.waitForTimeout(150);
  await page.selectOption('#grpTpl', '');
  await page.fill('#grpAddr', '127.0.0.1');
  await page.fill('#grpPort', '9');
  await modalSave(page);
  await page.waitForTimeout(1500);
  check('Add group: the other groups still take the installation\'s template',
        JSON.stringify(await own()) === '["",""]', JSON.stringify(await own()));
  });
  await fetch(`${BASE}/api/endpoints/e2ebank`, { method: 'DELETE' });

  // ── P18: a totals list the operator opened stays open across the tick ──
  await section('Totals open', async page => {
  await toEndpoint(page);
  await page.evaluate(ep => window.app._endpointPutPartial(ep, { totals: { e2e_sum: { op: 'sum', from: ['power_active_total'] } } }), EP);
  await page.waitForTimeout(800);
  try {
    await openEndpoint(page);                   // drawn with its total
    const det = page.locator('#plTotals details').first();
    await det.evaluate(d => { d.open = true; });
    await page.waitForTimeout(6000);
    check('totals: an opened list stays open across the 5 s refresh', await det.evaluate(d => d.open));
  } finally {
    await page.evaluate(ep => window.app._endpointPutPartial(ep, { totals: {} }), EP);
  }
  });

  // ── P16: a unit's Calculated tab says the value is that unit's alone ──
  await section('Calculated', async page => {
  await page.evaluate(() => window.app.openDeviceDetail('sunfield-u1').then(() => window.app._switchDeviceTab('calculated')));
  await page.waitForSelector('#calcUnitHint');
  const hint = await page.innerText('#calcUnitHint');
  check('Calculated on a unit: names the installation, says it is this unit\'s alone',
        /Sunfield PV/.test(hint) && /alone|doar/.test(hint), hint);
  await page.locator('#calcUnitHint button:has-text("installation")').click();
  await page.waitForSelector('[data-endpoint-page]');
  check('Calculated on a unit: "Open installation" opens it', true);
  });

  // ── P17: the PQ tab shows this device's status only, and its real URL ──
  await section('PQ tab', async page => {
  await page.route('**/api/pq/status', r => r.fulfill({ json: { devices: [
    { device: 'umg512', enabled: true, counters: { events: 4242, transients: 7 } }] } }));
  await page.evaluate(() => window.app.openDeviceDetail('pq1').then(() => window.app._switchDeviceTab('pq')));
  await page.waitForTimeout(1500);
  const pqInfo = await page.locator('#pqInfo').innerText().catch(() => '');
  check('PQ tab: another device\'s counters are never shown', !/4242/.test(pqInfo) && /No recorder status|status/i.test(pqInfo), pqInfo);
  const pqHint = await page.locator('#ddvPqBaseUrlHint').innerText().catch(() => '');
  check('PQ tab: a blank Base URL says the device\'s own address', /http:\/\/127\.0\.0\.1/.test(pqHint), pqHint);
  await page.route('**/api/pq/status', r => r.fulfill({ json: { devices: [{ device: 'pq1', enabled: false }] } }));
  await page.evaluate(() => window.app._loadPqStatus());
  await page.waitForTimeout(500);
  check('PQ tab: "off" points at the switch on this tab', /above|mai sus/.test(await page.locator('#pqInfo').innerText()),
        await page.locator('#pqInfo').innerText());
  });

  // ── the wizard, against a datalogger that answers (mocked) ──
  await section('wizard', async page => {
  let asked = 0;
  await page.route('**/api/fronius/discover**', r => {
    asked++;
    r.fulfill({ json: { inverters: [{ solar_api_id: 1, dt: 123 }, { solar_api_id: 2, dt: 123 }],
                        meters: [{ solar_api_id: 0, model: 'Smart Meter TS 65A-3', modbus_unit_hint: 240 }] } });
  });
  const wiz = async (ways, host) => {
    await page.evaluate(() => window.app.openPlantWizard());
    await page.waitForSelector('#pwName');
    await page.fill('#pwName', 'E2E Plant');
    await page.click(`label.seg-btn:has(input[name="pwWays"][value="${ways}"])`);
    await page.waitForTimeout(150);
    await page.fill('#pwHost', host);
    await page.click('#plantWizNext');
    await page.waitForTimeout(700);
  };
  const next = async () => { await page.click('#plantWizNext'); await page.waitForTimeout(500); };
  const back = async () => { await page.click('#plantWizBack'); await page.waitForTimeout(300); };
  const feedback = () => page.innerText('#plantWizFeedback');

  // P6: Back → Next keeps step 2
  await wiz('both', '127.0.0.1');
  const siteBox = page.locator('#plantWizBody input[data-f="on"]').nth(1);
  await siteBox.uncheck();
  await page.click('[data-action="plantWizAddGroup"]');
  await page.waitForTimeout(300);
  const manual = await page.$$eval('#plantWizBody [data-f="units"]', xs => xs.map(x => x.dataset.g));
  const mi = manual[manual.length - 1];
  await page.selectOption(`#pwRole${mi}`, 'battery');
  await page.waitForTimeout(200);
  await page.fill(`#pwUnits${mi}`, '7');
  await back();
  const asked1 = asked;
  await next();
  check('wizard: Back → Next keeps an unticked group unticked',
        !(await page.locator('#plantWizBody input[data-f="on"]').nth(1).isChecked()));
  check('wizard: Back → Next keeps a group added by hand',
        (await page.inputValue(`#pwUnits${mi}`).catch(() => '')) === '7');
  check('wizard: Back → Next asks the datalogger nothing new', asked === asked1, `${asked1} → ${asked}`);
  await back();
  await page.fill('#pwHost', '127.0.0.2');
  await next();
  check('wizard: a new address asks the datalogger again, the hand-made group stays',
        asked === asked1 + 1 && (await page.locator('#plantWizBody [data-f="units"]').count()) >= 1, `asked ${asked}`);

  await back();
  await page.click('label.seg-btn:has(input[name="pwWays"][value="modbus"])');
  await page.waitForTimeout(150);
  await page.fill('#pwHost', '127.0.0.3');
  await next();
  check('wizard: Modbus only at a new address shows nothing found at the old one',
        !(await page.locator('#plantWizBody input[data-f="on"]').count())
        && !/Found on the datalogger/.test(await page.innerText('#plantWizBody')));
  check('wizard: … and the group added by hand is still there',
        (await page.$$eval('#plantWizBody [data-f="units"]', xs => xs.map(x => x.value))).includes('7'));

  // P8: templates by declared kind (the hand-made group, wherever it is now)
  const mi2 = await page.$$eval('#plantWizBody [data-f="units"]', xs => xs.find(x => x.value === '7')?.dataset.g);
  const opts = await page.$$eval(`#pwTpl${mi2} option`, os => os.map(o => ({ v: o.value, grp: o.parentElement.tagName })));
  await page.selectOption(`#pwRole${mi2}`, 'inverter');
  await page.waitForTimeout(300);
  const invOpts = await page.$$eval('#plantWizBody select[data-f="template"] option', os =>
    os.map(o => ({ v: o.value, grp: o.parentElement.tagName })));
  check('wizard: an inverter group offers the inverter template',
        invOpts.some(o => o.v === 'fronius_sunspec_inverter' && o.grp === 'SELECT'));
  check('wizard: never a template of another declared kind', !invOpts.some(o => o.v === 'eastron_sdm630'));
  check('wizard: a template of no declared kind only under its own heading',
        invOpts.some(o => o.v === 'delta_vfd_el' && o.grp === 'OPTGROUP') && !invOpts.some(o => o.v === 'delta_vfd_el' && o.grp === 'SELECT'));
  check('wizard: a battery group offers no meter', !opts.some(o => o.v === 'eastron_sdm630'));
  await page.evaluate(() => window.app.closeModal('plantWizardModal'));

  // P7: the meter keeps its Modbus unit; over Solar API alone it is said, not dropped
  await wiz('both', '127.0.0.1');
  const boxes = page.locator('#plantWizBody input[data-f="on"]');
  await boxes.nth(2).check();                 // the Smart Meter
  await next();
  check('wizard (both ways): the meter on unit 240 does not collide with the site', !/claimed by two/.test(await feedback()), await feedback());
  await next();
  const review = await page.innerText('#plantWizBody');
  check('wizard (both ways): the review lists the meter as e2e-plant-meter-240', /e2e-plant-meter-240/.test(review), review.slice(0, 300));
  await page.evaluate(() => window.app.closeModal('plantWizardModal'));
  await wiz('http', '127.0.0.1');
  await page.locator('#plantWizBody input[data-f="on"]').nth(2).check();
  await next();
  // with fronius_solar_api_meter bundled, the meter is read over the Solar API
  const fb = await feedback();
  check('wizard (Solar API only): the meter is read over the Solar API, not refused',
        !/untick/i.test(fb) && !/claimed by two/.test(fb), fb);
  const meterUnits = await page.evaluate(() => window.app._plantWiz.data.groups.find(g => g.role === 'meter')?.units);
  check('wizard (Solar API only): the meter keeps its Modbus unit', meterUnits === '240', meterUnits);
  // the datalogger is shared: the meter's one request defaults to 5 s, not 2,
  // and the step says what the installation asks of the datalogger
  const gi = await page.evaluate(() => window.app._plantWiz.data.groups.findIndex(g => g.role === 'meter'));
  const meterEvery = await page.inputValue(`#plantWizBody input[data-g="${gi}"][data-f="httpEvery"]`);
  check('wizard (Solar API only): the meter is read every 5 s by default, in one request',
        meterEvery === '5' && !(await page.locator(`#plantWizBody input[data-g="${gi}"][data-f="energyEvery"]`).count()), meterEvery);
  const load1 = await page.innerText('#pwLoad');
  check('wizard: the step says the requests per minute to the datalogger', /Solar API requests\/min/.test(load1), load1);
  await page.fill(`#plantWizBody input[data-g="${gi}"][data-f="httpEvery"]`, '1');
  const load2 = await page.innerText('#pwLoad');
  check('wizard: reading too often is said as over the datalogger\'s budget', /more than it is known to sustain/.test(load2), load2);
  await page.fill(`#plantWizBody input[data-g="${gi}"][data-f="httpEvery"]`, '5');
  await next();
  const review2 = await page.innerText('#plantWizBody');
  check('wizard (Solar API only): the review lists the meter as e2e-plant-meter-240', /e2e-plant-meter-240/.test(review2), review2.slice(0, 300));
  await page.evaluate(() => window.app.closeModal('plantWizardModal'));
  });

  // ── P14: a virtual meter is added with the fields Edit has; its switch is the meter ──
  await section('virtual meter', async page => {
  await fetch(`${BASE}/api/virtual-meters/template/e2e_vm`, json('PUT', {
    name: 'E2E meter', byte_order: 'big', port: 1502, unit_id: 1,
    registers: [{ addr: '0x0000', type: 'int32', source_kind: 'live', source: 'power_active_total', scale: 10 }] }));
  await page.evaluate(() => window.app.navigateTo('vmeters'));
  await page.waitForTimeout(1200);
  await page.click('#vmAddInstanceBtn');
  await page.waitForSelector('#vmAddInstanceModal.active');
  check('Add virtual meter: stale after, refresh and HA mode, as on Edit',
        await page.isVisible('#vmAddStale') && await page.isVisible('#vmAddInterval') && await page.isVisible('#vmAddHaMode'));
  await page.fill('#vmAddUnit', '0');
  await page.fill('#vmAddInterval', '5');
  await page.selectOption('#vmAddHaMode', 'balanced');
  await page.click('#vmAddInstanceModal [data-action="submitAddInstance"]');
  await page.waitForTimeout(1500);
  const inst = ((await api('/api/virtual-meters')).instances || []).find(i => i.template === 'e2e_vm');
  check('Add virtual meter: refresh 5 s, balanced, unit 0 kept',
        inst && inst.update_interval_s === 5 && inst.ha_mode === 'balanced' && inst.unit_id === 0, JSON.stringify(inst));
  await page.waitForTimeout(800);
  await page.route('**/api/virtual-meters/e2e_vm/toggle**', r =>
    r.fulfill({ status: 400, json: { detail: 'failed to start (enable reverted)' } }));
  const sw = page.locator('input[data-vm="e2e_vm"]');
  await page.locator('label.toggle-switch:has(input[data-vm="e2e_vm"])').click();
  await page.waitForTimeout(800);
  const toast = await page.locator('.toast').allInnerTexts().then(x => x.join(' | '));
  check('virtual meter switch: a refused start leaves it off and says why',
        !(await sw.isChecked()) && /failed to start/.test(toast), toast);
  if (OUT) await page.screenshot({ path: `${OUT}/installations-step3.png` });
  });

  check('the group `extra` is gone again', !(await api(`/api/endpoints/${EP}`)).groups.some(g => g.id === 'extra'));
  check('no page errors', pageErrs.length === 0, pageErrs.join(' | '));
  check('no native dialog', natives === 0);
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await fetch(`${BASE}/api/virtual-meters/e2e_vm`, { method: 'DELETE' }).catch(() => {});
  await fetch(`${BASE}/api/virtual-meters/template/e2e_vm`, { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
