// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// One way to edit a device (ADR-0032 §6, L3.11b), as an administrator meets it:
// A  the primary meter (umg512) opens in the Edit device modal — Connection,
//    Polling, Identity, Outputs, no Model; no page form, no "Save & Apply";
//    no Advanced connection keys for it
// B  its fixed fields say why: acquisition always on, its name, MQTT /
//    InfluxDB / HA locked — the HA lock leads to Settings → MQTT
// C  an interval of the primary: one PATCH (polling.poll_groups), no POST of
//    its own to /poll-groups
// D  Identity: a description saved with the same Save, under the attributes'
//    revision (If-Match); read back
// E  the PQ recorder: staged with Outputs, no button of its own; Save sends
//    /api/pq/config after nothing else changed
// F  a unit (pv-u1): Rename… → the modal at Identity, the name focused;
//    Connection says "set on the installation PV" with the way there; the
//    name is saved by the unit route
// G  a unit's Outputs: its installation's, staged, said so — no button of
//    their own
// H  a component (gx-battery-288): Rename… → the modal; its on/off is held
//    while its device is off (L3.10-F9), saying why; the name by its
//    device's route
// I  Measurements: full screen, the edit bar with one Save; a tick sends
//    nothing until Save; Save writes the selection once
// J  the old stand-alone register editor (the dashboard's shortcut) is that
//    same editor, on the device page
// K  Commands & bindings: full screen, the edit bar
// L  E on a unit's Overview opens the modal at Identity
// the review (2026-11-01):
// M  (M1) a full editor's staged edits are asked about however the page is
//    left: Back, a hash change, a nav tab, another device, Back to the list
// N  (M2) a 412 in the full Measurements editor: the stored selection read
//    again, the staged tick re-applied over it; the next Save goes through
// O  (m1) Tab stays in the full editor, Esc asks, Done/Esc ends on ⋯
// P  (m6) a refusal after a partial save is said at its field
// Q  (m9) a unit's REST push on without a URL is refused before any write
// R  (m3) a unit's installation-wide output is said by its sibling at once
// the second review: S (R2-1) a link inside the modal asks; T (R2-2) a
//    navigation with the modal's edits asks; U (R2-3) one question for one move
//
// Against a throwaway gateway: installation_safety/base.yaml with
// unchanged_save/config-extra.yaml merged in (a primary, a GX with a
// component, an installation `pv` with units pv-u1/pv-u2; nothing real is
// reached). It puts back what it changes.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18451 node tools/e2e/one_edit_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { editorSection, editorSettled, editorModalOpen, closeDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18451';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();
const etagOf = async id => (await fetch(`${BASE}/api/devices/${id}`)).headers.get('etag');

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(12000);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
const dlg = await watchDialogs(page);
// every write the page sends: [method, path]
const writes = [];
page.on('request', r => {
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(r.method())) writes.push([r.method(), new URL(r.url()).pathname + new URL(r.url()).search]);
});
const wrote = (m, re) => writes.some(([mm, p]) => mm === m && re.test(p));

async function openDevice(id) {
  await page.evaluate(i => window.app.openDeviceDetail(i), id);
  await page.waitForFunction(i => window.app._devDetail?.id === i, id);
  await page.waitForSelector('#ddvActions');
}
async function menu(item) {
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
  const el = page.locator(`#ddvActionsMenu [data-ddv-act="${item}"]`);
  await el.click({ force: (await el.getAttribute('aria-disabled')) === 'true' });
}
const modalOpen = () => editorModalOpen(page);
const rail = async () => page.$$eval('#devEditRail [data-shell-sec]', els => els.map(e => e.dataset.shellSec));
async function save() {
  await editorSettled(page);
  await page.click('#devEditSave');
  await page.waitForFunction(() => !document.querySelector('#devEditModal.active'), null, { timeout: 10000 }).catch(() => {});
}

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.waitForFunction(() => !!window.app);

  // ── A: the primary in the modal ──
  await openDevice('umg512');
  check('A no page form on the primary (no Save & Apply)', !(await page.locator('[data-action="saveDeviceDetail"]').count()));
  await menu('connection');
  await page.waitForTimeout(400);
  check('A Edit connection… on the primary opens the Edit device modal', await modalOpen());
  const secs = await rail();
  check('A its sections: Connection, Polling, Identity, Outputs — no Model', ['connection', 'polling', 'identity', 'outputs'].every(s => secs.includes(s)) && !secs.includes('model'), secs);
  check('A no Advanced connection keys for the primary', !(await page.locator('#devEditModal details.cs-adv').count()));

  // ── B: the fixed fields say why ──
  if (await modalOpen()) {
    await editorSection(page, 'polling');
    const en = page.locator('#ddvEnabled');
    check('B acquisition on/off is held for the primary, focusable, with its reason',
          (await en.getAttribute('aria-disabled')) === 'true' && /always polls/i.test(await page.locator('#devEditPanel-polling').innerText()));
    await editorSection(page, 'identity');
    check('B its name is held, with the reason', (await page.locator('#ddvName').getAttribute('aria-disabled')) === 'true'
          && /name is fixed/i.test(await page.locator('#devEditPanel-identity').innerText()));
    await editorSection(page, 'outputs');
    await page.click('#outTab-mqtt').catch(() => {});
    const ha = page.locator('#ddvHaDisc');
    const haWay = page.locator('#devEditPanel-outputs [data-lock-way="mqtt"]');
    check('B the HA switch is held: the gateway\'s own, with the way to Settings → MQTT',
          (await ha.getAttribute('aria-disabled')) === 'true' && (await haWay.count()) > 0);
    // ── C: an interval — one PATCH ──
    await editorSection(page, 'polling');
    const pg = page.locator('#devEditPanel-polling .ddv-pg').first();
    const pgName = await pg.getAttribute('data-group');
    const pgWas = await pg.inputValue();
    await pg.fill(String(Number(pgWas) + 1));
    writes.length = 0;
    await save();
    check('C an interval of the primary: one PATCH with polling.poll_groups, no POST to /poll-groups',
          wrote('PATCH', /\/api\/devices\/umg512$/) && !wrote('POST', /poll-groups/), writes);
    const g = (await api('/api/devices/umg512/poll-groups')).poll_groups || {};
    check('C stored', Number((g[pgName] || {}).interval) === Number(pgWas) + 1, g[pgName]);
    // put it back
    await fetch(`${BASE}/api/devices/umg512`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'If-Match': await etagOf('umg512') },
      body: JSON.stringify({ sections: { polling: { poll_groups: { [pgName]: { interval: Number(pgWas) } } } } }) });
  }

  // ── D: identity with its attributes, one Save ──
  await openDevice('umg512');
  await menu('edit');
  await page.waitForSelector('#devEditModal.active');
  await editorSection(page, 'identity');
  const desc = page.locator('[data-draft-path="about.description"]');
  check('D the Identity section holds the description, labels and nameplate',
        (await desc.count()) === 1 && (await page.locator('[data-draft-path="about.manufacturer"]').count()) === 1
        && (await page.locator('[data-draft-path="about.labels"]').count()) === 1);
  if (await desc.count()) {
    await desc.fill('Main incomer meter');
    writes.length = 0;
    await save();
    const a = await api('/api/attributes/devices/umg512');
    check('D the description saved with Save, by the attributes route', wrote('PUT', /\/api\/attributes\/devices\/umg512\/identity$/)
          && ((a.attributes || {}).identity || {}).description === 'Main incomer meter', writes);
    await fetch(`${BASE}/api/attributes/devices/umg512/identity`, { method: 'DELETE', headers: { 'If-Match': a.rev || '' } });
  } else if (await modalOpen()) await closeDeviceEditor(page);

  // ── E: the PQ recorder, staged ──
  await openDevice('umg512');
  const pqOn = await page.evaluate(() => !!window.app._devDetail?.data?.pq_supported);
  if (pqOn) {
    await menu('outputs');
    await page.waitForSelector('#devEditModal.active');
    check('E the PQ recorder has no button of its own in the modal', !(await page.locator('#devEditModal [data-action="savePqRecorder"]').count()));
    const poll = page.locator('#devEditModal [data-draft-path="pq.poll_s"]');
    if (await poll.count()) {
      const was = await poll.inputValue();
      await poll.fill(String(Number(was) + 60));
      writes.length = 0;
      await save();
      check('E the PQ settings saved with Save', wrote('POST', /\/api\/pq\/config/), writes);
      await page.evaluate(async v => {
        const s = window.app._devDetail;
        await window.DeviceStore.devWrite(s.id, 'POST', `/api/pq/config?device=${encodeURIComponent(s.id)}`, { ...(s.entry?.pq_recorder || {}), poll_s: v });
      }, Number(was));
    } else {
      check('E the PQ settings are in the draft', false, 'no [data-draft-path="pq.poll_s"]');
      await closeDeviceEditor(page);
    }
  } else check('E (the primary here has no PQ recorder)', true);

  // ── F: a unit ──
  await openDevice('pv-u1');
  const uName = await page.evaluate(() => window.app._devDetail.data.name);
  await menu('rename');
  await page.waitForTimeout(400);
  check('F Rename… on a unit opens the Edit device modal at Identity, the name focused',
        await modalOpen() && await page.evaluate(() => document.activeElement?.id) === 'ddvName');
  if (await modalOpen()) {
    await editorSection(page, 'connection');
    const conn = await page.locator('#devEditPanel-connection').innerText();
    check('F its connection: set on the installation PV, with the way there',
          /PV/.test(conn) && (await page.locator('#devEditPanel-connection [data-action="openEndpointDetail"], #devEditPanel-connection [data-de-way="installation"]').count()) > 0, conn);
    await editorSection(page, 'identity');
    await page.fill('#ddvName', 'Inverter one (e2e)');
    writes.length = 0;
    await save();
    check('F the name by the unit route', wrote('PATCH', /\/api\/endpoints\/pv\/units\/pv-u1$/), writes);
    check('F stored', (await api('/api/devices/pv-u1')).device?.name === 'Inverter one (e2e)');
    await page.evaluate(async n => {
      await window.DeviceStore.devWrite('pv-u1', 'PATCH', '/api/endpoints/pv/units/pv-u1', { name: n });
    }, uName === 'pv-u1' ? '' : uName);
  }
  // ── G: a unit's outputs ──
  await openDevice('pv-u1');
  await menu('outputs');
  await page.waitForTimeout(400);
  check('G Outputs… on a unit opens the modal at Outputs', await modalOpen() && !(await page.locator('#devEditPanel-outputs[hidden]').count()));
  if (await modalOpen()) {
    const txt = await page.locator('#devEditPanel-outputs').innerText();
    check('G said to be the installation\'s, for every unit', /PV/.test(txt) && /every unit/i.test(txt), txt.slice(0, 200));
    check('G staged: no button of their own', !(await page.locator('#devEditModal [data-action="saveRestPush"], #devEditModal [data-action="savePublishing"], #devEditModal [data-action="saveOpcuaFields"]').count()));
    await closeDeviceEditor(page);
  }

  // ── H: a component ──
  await openDevice('gx-battery-288');
  await menu('rename');
  await page.waitForTimeout(400);
  check('H Rename… on a component opens the Edit device modal', await modalOpen());
  if (await modalOpen()) {
    await editorSection(page, 'polling');
    const en = page.locator('#ddvEnabled');
    check('H its on/off is held while its device is off, saying so (L3.10-F9)',
          (await en.getAttribute('aria-disabled')) === 'true' && /Victron GX/.test(await page.locator('#devEditPanel-polling').innerText()));
    await editorSection(page, 'identity');
    const cName = await page.inputValue('#ddvName');
    await page.fill('#ddvName', 'Shunt (e2e)');
    writes.length = 0;
    await save();
    check('H the name by its device\'s route', wrote('PATCH', /\/api\/devices\/gx\/components\/gx-battery-288$/), writes);
    await page.evaluate(async n => {
      await window.DeviceStore.devWrite('gx', 'PATCH', '/api/devices/gx/components/gx-battery-288', { name: n });
    }, cName);
  }

  // ── I: Measurements, full screen, one Save ──
  await openDevice('rs');
  await menu('measurements');
  await page.waitForTimeout(600);
  check('I Measurements… opens full screen with the edit bar and its one Save',
        await page.locator('#deviceDetailView.ddv-full').count() === 1 && await page.locator('#ddvEditSave').isVisible());
  if (await page.locator('#ddvEditSave').isVisible()) {
    await page.click('#deviceRegTabs [data-regtab="available"]');
    await page.waitForSelector('#registersTableBody tr');
    writes.length = 0;
    const tick = page.locator('#registersTableBody input[type="checkbox"]:not(:checked)').first();
    const tickName = await tick.getAttribute('aria-label');
    await tick.evaluate(el => el.click());               // the row is drawn again on change
    await page.waitForTimeout(500);
    check('I a tick sends nothing until Save', !wrote('POST', /\/api\/registers\/selected/), writes);
    await page.click('#ddvEditSave');
    await page.waitForTimeout(800);
    check('I Save writes the selection once', writes.filter(([m, p]) => m === 'POST' && /\/api\/registers\/selected/.test(p)).length === 1, writes);
    // put it back: untick, save
    await page.locator(`#registersTableBody input[type="checkbox"][aria-label="${tickName}"]`).first().evaluate(el => el.click()).catch(() => {});
    await page.click('#ddvEditSave');
    await page.waitForTimeout(500);
    await page.click('#ddvEditDone');
  }

  // ── J: the old stand-alone register editor = the same editor ──
  await page.evaluate(() => window.app.jumpToDeviceRegisters('rs'));
  await page.waitForTimeout(800);
  check('J the dashboard\'s shortcut opens the device page\'s full-screen editor',
        await page.evaluate(() => window.app._devDetail?.id === 'rs') && await page.locator('#deviceDetailView.ddv-full').count() === 1);
  if (await page.locator('#ddvEditDone').isVisible()) await page.click('#ddvEditDone');

  // ── K: Commands & bindings, full screen ──
  await openDevice('pv-u1');
  const hasCmds = await page.evaluate(() => (window.app._devDetail?.data?.commands_offered || []).length > 0);
  if (hasCmds) {
    await page.click('#ddvActions');
    await page.waitForSelector('#ddvActionsMenu:not([hidden])');
    check('K a unit: Commands & bindings… held — its installation\'s group enables them',
          (await page.getAttribute('#ddvActionsMenu [data-ddv-act="commands"]', 'aria-disabled')) === 'true');
    await page.keyboard.press('Escape');
  }
  await openDevice('hp');
  const hpCmds = await page.evaluate(() => (window.app._devDetail?.data?.commands_offered || []).length > 0);
  if (hpCmds) {
    await menu('commands');
    await page.waitForTimeout(600);
    check('K Commands & bindings… opens full screen with the edit bar and its one Save',
          await page.locator('#deviceDetailView.ddv-full').count() === 1 && await page.locator('#ddvEditSave').isVisible());
    if (await page.locator('#ddvEditDone').isVisible()) await page.click('#ddvEditDone');
  }

  // ── L: E on a unit ──
  await openDevice('pv-u1');
  await page.locator('#dtab-overview').click();
  await page.keyboard.press('Escape');
  await page.locator('body').click({ position: { x: 5, y: 5 } }).catch(() => {});
  await page.keyboard.press('e');
  await page.waitForTimeout(500);
  check('L E on a unit\'s Overview opens the modal at Identity', await modalOpen() && !(await page.locator('#devEditPanel-identity[hidden]').count()));
  if (await modalOpen()) await closeDeviceEditor(page);

  // ── the review (2026-11-01) ──
  // M1: a full editor's staged edits are asked about however the page is left
  const stageTick = async () => {
    await openDevice('rs');
    await menu('measurements');
    await page.waitForSelector('#deviceDetailView.ddv-full');
    await page.click('#deviceRegTabs [data-regtab="available"]');
    await page.waitForSelector('#registersTableBody tr');
    await page.locator('#registersTableBody input[type="checkbox"]:not(:checked)').first().evaluate(el => el.click());
    await page.waitForTimeout(300);
    return page.evaluate(() => window.app._ddvFullDirty());
  };
  const asked = () => dlg.seen.length;
  for (const [how, leave] of [
    ['Back', () => page.goBack()],
    ['a hash change', () => page.evaluate(() => { location.hash = '#/devices'; })],
    ['a nav tab', () => page.evaluate(() => window.app.navigateTo('dashboard', { fromNav: true }))],
    ['another device', () => page.evaluate(() => window.app.openDeviceDetail('hp'))],
    ['Back to the list', () => page.evaluate(() => window.app.closeDeviceDetail(true))],
  ]) {
    if (how === 'Back') { await page.evaluate(() => { location.hash = '#/devices/udp'; }); await page.waitForTimeout(800); }
    const dirty = await stageTick();
    const n = asked();
    dlg.answers.push(false);                                   // Keep editing
    await leave();
    await page.waitForTimeout(900);
    const kept = await page.evaluate(() => window.app._devDetail?.id === 'rs' && window.app._ddvFullDirty()
      && document.getElementById('deviceDetailView').classList.contains('ddv-full'));
    check(`M1 ${how}: asked first; Keep editing keeps the editor and its staged tick`, dirty && asked() === n + 1 && kept,
          `${dirty} ${asked() - n} ${kept}`);
    dlg.answers.push(true);                                    // Discard
    await page.click('#ddvEditDone');
    await page.waitForTimeout(800);
  }
  // N (M2): a 412 in the full Measurements editor — the stored selection read
  // again, the staged change re-applied over it, the next Save goes through
  {
    await stageTick();
    const staged = await page.evaluate(() => window.app.selectedRegisters.map(r => window.app._rk(r)));
    const sel = await (await fetch(`${BASE}/api/registers/selected?device=rs`)).json();
    const keep = (sel.registers || []).filter(r => r.address < 900000);
    // another tab saves the selection meanwhile: one row of the catalog more, not the one ticked here
    const theirs = await page.evaluate(ticked => {
      const row = window.app.flattenRegisters().find(r => !ticked.includes(window.app._rk(r)));
      return { address: row.address, name: row.name, label: row.label || row.name, unit: row.unit || '',
               data_type: row.data_type || 'float', poll_group: 'normal' };
    }, staged);
    await fetch(`${BASE}/api/registers/selected?device=rs`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify([...keep, theirs]) });
    await page.click('#ddvEditSave');
    await page.waitForTimeout(1500);
    const fbN = await page.locator('#ddvEditFeedback').innerText();
    const after = await page.evaluate(() => ({ dirty: window.app._ddvFullDirty(), keys: window.app.selectedRegisters.map(r => window.app._rk(r)) }));
    check('N a 412 in the full editor: said, my tick re-applied over the stored selection (their row taken)',
          /changed elsewhere|re-applied/i.test(fbN) && after.dirty && staged.every(k => after.keys.includes(k))
          && after.keys.length === staged.length + 1, `${fbN} ${JSON.stringify(after)} staged=${staged}`);
    await page.click('#ddvEditSave');
    await page.waitForTimeout(1500);
    const saved = (await (await fetch(`${BASE}/api/registers/selected?device=rs`)).json()).registers || [];
    check('N …the next Save goes through (no 412 again): both rows stored', !(await page.evaluate(() => window.app._ddvFullDirty()))
          && saved.filter(r => r.address < 900000).length === staged.length + 1, String(saved.length));
    await fetch(`${BASE}/api/registers/selected?device=rs`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(keep) });
    await page.click('#ddvEditDone');
    await page.waitForTimeout(600);
    if (await page.locator('#appDialog.active').count()) await page.keyboard.press('Enter');
  }
  // O (m1): the full editor keeps the focus, Esc asks, Done returns to ⋯
  {
    await stageTick();
    for (let i = 0; i < 40; i++) await page.keyboard.press('Tab');
    const inside = await page.evaluate(() => document.getElementById('deviceDetailView').contains(document.activeElement)
      || document.activeElement === document.body);
    check('O Tab never leaves the full editor (the page behind is inert)', inside, await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 80)));
    const n = asked();
    dlg.answers.push(false);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(500);
    check('O Esc asks about the staged edits (Keep editing stays)', asked() === n + 1
          && await page.locator('#deviceDetailView.ddv-full').count() === 1);
    dlg.answers.push(true);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(900);
    check('O Esc, Discard: the view, the focus on ⋯', await page.locator('#deviceDetailView.ddv-full').count() === 0
          && await page.evaluate(() => document.activeElement?.id === 'ddvActions'));
  }
  // P (m6): a refusal after a partial save is said at its field
  {
    await openDevice('umg512');
    await menu('edit');
    await page.waitForSelector('#devEditModal.active');
    await editorSection(page, 'polling');
    const pg = page.locator('#devEditPanel-polling .ddv-pg').first();
    const pgName = await pg.getAttribute('data-group');
    const pgWas = await pg.inputValue();
    await pg.fill(String(Number(pgWas) + 1));
    await editorSection(page, 'identity');
    await page.fill('[data-draft-path="about.labels"]', 'Bad Label!!');
    await editorSettled(page);
    await page.click('#devEditSave');
    await page.waitForTimeout(1500);
    const err = await page.locator('#devEditAbout-labels-err').innerText().catch(() => '');
    check('P a partial save: the attributes\' refusal at its field (the interval saved)', !!err && await modalOpen(), err);
    await page.click('#devEditCancel');
    await page.waitForTimeout(300);
    if (await page.locator('#appDialog.active').count()) await page.keyboard.press('Enter');
    await fetch(`${BASE}/api/devices/umg512`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'If-Match': await etagOf('umg512') },
      body: JSON.stringify({ sections: { polling: { poll_groups: { [pgName]: { interval: Number(pgWas) } } } } }) });
  }
  // Q (m9): a unit's REST push on without a URL — refused here, nothing written
  {
    await openDevice('pv-u2');
    await menu('outputs');
    await page.waitForSelector('#devEditModal.active');
    await page.click('#outTab-rest');
    await page.waitForTimeout(200);
    await editorSection(page, 'identity');
    await page.fill('#ddvName', 'Unit two (e2e)');
    await editorSection(page, 'outputs');
    await page.click('#outTab-rest');
    await page.locator('#outEnabled').click();
    await editorSettled(page);
    writes.length = 0;
    await page.click('#devEditSave', { force: true });
    await page.waitForTimeout(1000);
    check('Q a unit\'s REST push without a URL: refused at its field before any write', !writes.length
          && /URL/i.test(await page.locator('#devEditErrors').innerText()), JSON.stringify(writes));
    await page.click('#devEditCancel');
    await page.waitForTimeout(300);
  }
  // R (m3): a unit's installation-wide output, saved — its sibling says it at once
  {
    const before = (await api('/api/devices')).devices.find(d => d.id === 'pv-u2').http_output_enabled;
    await openDevice('pv-u1');
    await menu('outputs');
    await page.waitForSelector('#devEditModal.active');
    await page.click('#outTab-http');
    await page.locator('#outEnabled').click();
    await save();
    const after = (await api('/api/devices')).devices.find(d => d.id === 'pv-u2').http_output_enabled;
    check('R the sibling unit says the installation\'s HTTP output at once', after === !before, `${before} → ${after}`);
    await page.evaluate(async on => { await window.DeviceStore.devWrite('pv-u1', 'POST', '/api/devices/pv-u1/http-output', { enabled: on }); }, before);
  }

  // ── the second review ──
  // S (R2-1): a link inside the Edit modal asks before it drops the edits
  {
    await openDevice('gx-battery-288');
    await menu('rename');
    await page.waitForSelector('#devEditModal.active #ddvName');
    const was = await page.inputValue('#ddvName');
    await page.fill('#ddvName', 'Typed, not saved');
    await editorSection(page, 'connection');
    const link = page.locator('#devEditPanel-connection a[data-action="openDeviceDetail"]').first();
    let n = asked();
    dlg.answers.push(false);
    await link.click();
    await page.waitForTimeout(800);
    check('S a component\'s device link in the modal: asked; Keep editing keeps the modal and the name',
          asked() === n + 1 && await modalOpen() && (await page.inputValue('#ddvName')) === 'Typed, not saved',
          `${asked() - n} ${await modalOpen()}`);
    n = asked();
    dlg.answers.push(true);
    await link.click();
    await page.waitForTimeout(1200);
    check('S …Discard: its device opens, asked once, nothing saved', asked() === n + 1 && !(await modalOpen())
          && await page.evaluate(() => window.app._devDetail?.id) === 'gx'
          && (await api('/api/devices')).devices.find(d => d.id === 'gx-battery-288').name === was);

    await openDevice('pv-u1');
    await menu('outputs');
    await page.waitForSelector('#devEditModal.active');
    await page.click('#outTab-http');
    await page.locator('#outEnabled').click();
    const inst = page.locator('#devEditPanel-outputs .outputs-unit-note [data-action="openEndpointDetail"]').first();
    n = asked();
    dlg.answers.push(false);
    await inst.click();
    await page.waitForTimeout(800);
    check('S a unit\'s "Open installation" in the modal: asked; Keep editing keeps it', asked() === n + 1 && await modalOpen());
    n = asked();
    dlg.answers.push(true);
    await inst.click();
    await page.waitForTimeout(1500);
    check('S …Discard: the installation opens, asked once', asked() === n + 1 && !(await modalOpen())
          && await page.locator('[data-endpoint-page]').count() === 1);
  }
  // T (R2-2): a navigation with the modal's edits asks
  {
    await openDevice('rs');
    await menu('rename').catch(() => {});
    if (!(await modalOpen())) await menu('edit');
    await page.waitForSelector('#devEditModal.active');
    await editorSection(page, 'identity');
    await page.fill('#ddvName', 'Typed, not saved');
    let n = asked();
    dlg.answers.push(false);
    await page.evaluate(() => window.app.navigateTo('dashboard', { fromNav: true }));
    await page.waitForTimeout(800);
    check('T navigateTo with the modal\'s edits: asked; Keep editing keeps them', asked() === n + 1 && await modalOpen()
          && (await page.inputValue('#ddvName')) === 'Typed, not saved');
    n = asked();
    dlg.answers.push(true);
    await page.evaluate(() => window.app.navigateTo('dashboard', { fromNav: true }));
    await page.waitForTimeout(1000);
    check('T …Discard: the page goes on', asked() === n + 1 && !(await modalOpen())
          && await page.evaluate(() => window.app.currentPage) === 'dashboard');
  }
  // U (R2-3): Commands & bindings with a typed form — one question for one move
  {
    await openDevice('gx');
    await menu('commands');
    await page.waitForSelector('#deviceDetailView.ddv-full .cb-form input:not([disabled])');
    await page.locator('#deviceDetailView .cb-form input:not([disabled])').first().fill('77');
    const n = asked();
    dlg.answers.push(true);
    await page.evaluate(() => { location.hash = '#/devices/rs'; });
    await page.waitForTimeout(1500);
    check('U a typed binding, then another device: asked once, it opens', asked() === n + 1
          && await page.evaluate(() => window.app._devDetail?.id) === 'rs', `${asked() - n}`);
  }
} catch (e) {
  check('the run finished', false, String(e && e.stack || e));
}
check('no page error', !errs.length, errs.join(' | '));
await browser.close();
const bad = results.filter(r => !r.ok).length;
console.log(`\n${results.length - bad}/${results.length} passed`);
process.exit(bad ? 1 : 0);
