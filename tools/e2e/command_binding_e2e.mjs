/* A command's binding and its channel's pace, edited where an operator
 * looks for them (ADR-0019): the Commands tab of a device of its own (GX,
 * drive), read-only with a pointer on an installation's unit, the same
 * fields in the installation group's editor, and the status page's item for
 * a command lane falling behind (ADR-0018 D14). Every value round-trips:
 * saved, the page reloaded, the same in the form and in the API listing.
 *
 * Against the cmdui fixture (nothing answers; writes off — nothing is ever
 * written to a device, only bindings are saved):
 *
 *   MBG_FX_NAME=mbg-e2e-cmdui tools/e2e/fixture_up.sh cmdui 18197
 *   MBG_URL=http://127.0.0.1:18197 CHROMIUM_PATH=<chrome> node tools/e2e/command_binding_e2e.mjs
 *   docker rm -f mbg-e2e-cmdui
 *
 * SHOTS_DIR (optional) receives screenshots of each form.
 */
import { chromium } from 'playwright';
import { openDeviceTab, openDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18197';
const SHOTS = process.env.SHOTS_DIR || '';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`); };
const api = async (path) => (await fetch(BASE + path)).json();
const cmdOf = async (dev, name) => ((await api(`/api/devices/${dev}/commands`)).commands || []).find(c => c.name === name) || {};
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
page.setDefaultTimeout(20000);
// a 422 the test asks for on purpose is not a page error
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
const shot = async (loc, name) => { if (SHOTS) await loc.screenshot({ path: `${SHOTS}/${name}.png` }).catch(() => {}); };
// the writes the page sends (the editor is staged: nothing until its one Save, L3.11b)
const puts = []; page.on('request', r => { if (r.method() === 'PUT') puts.push(new URL(r.url()).pathname); });
// the full-screen editor's one Save (its edit bar): what it said
async function barSave() {
  await page.evaluate(() => { const f = document.getElementById('ddvEditFeedback'); if (f) f.textContent = ''; });
  await page.click('#ddvEditSave');
  await page.waitForFunction(() => /Saved|Not saved|Nothing/.test(document.getElementById('ddvEditFeedback')?.textContent || ''));
  return page.locator('#ddvEditFeedback').innerText();
}

const panel = page.locator('#deviceDetailView [data-dpanel="commands"]');
const bind = name => panel.locator(`.cb-bind[data-cb-name="${name}"]`);
const chan = cid => panel.locator(`.cb-chan[data-cb-cid="${cid}"]`);
async function openCommands(dev) {
  await page.goto(`${BASE}/#/devices/${dev}/commands`);
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  if (!(await page.locator('#deviceWsTabs [data-dtab="commands"].active').count())) await openDeviceTab(page, 'commands');
  await panel.locator('.cmd-card').first().waitFor();
  // the tab operates; its bindings, channels and Enabled ticks edit from
  // ⋯ → Commands & bindings… (L3.3) — a unit has none (its group sets them)
  // (L3.10) a unit has the item too, held: its installation's group enables its commands
  if (await page.locator('#ddvActionsMenu [data-ddv-act="commands"]:not([aria-disabled="true"])').count()) {
    viewSeen = viewSeen || { inputs: await panel.locator('.cb-form input:not([disabled])').count(),
                             saves: await panel.locator('[data-cb-save]').count(),
                             ticks: await panel.locator('[data-action="toggleDeviceCommand"]:visible').count(),
                             pointer: await panel.locator('[data-cb-pointer]').count() };
    await openDeviceEditor(page, 'commands');
    await panel.locator('.cmd-card').first().waitFor();
  }
}
let viewSeen = null;
async function reload(dev) { await page.goto('about:blank'); await openCommands(dev); }
// every input of the binding forms has a label, and what describes it exists
async function labelled(scope) {
  return scope.evaluate(root => [...root.querySelectorAll('.cb-form input')].every(i =>
    i.id && root.ownerDocument.querySelector(`label[for="${CSS.escape(i.id)}"]`)
    && (i.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean).every(id => root.ownerDocument.getElementById(id))));
}

try {
  // ── a device of its own: the GX ──
  await openCommands('gx');
  check('the Commands tab (the view) edits nothing: no field, Save or Enabled tick, no group pointer',
        viewSeen && !viewSeen.inputs && !viewSeen.saves && !viewSeen.ticks && !viewSeen.pointer, JSON.stringify(viewSeen));
  check('⋯ → Commands & bindings… opens them editing', await panel.locator('.cb-form input:not([disabled])').count() > 0);
  check('commands are grouped under their channel', await panel.locator('section.cb-group[data-channel-group="ess_setpoint"] .cmd-card[data-cmd="ess_setpoint"]').count() === 1);
  const ess = bind('ess_setpoint');
  check('full screen, one Save in its edit bar, none of each form (L3.11b)',
        await page.locator('#deviceDetailView.ddv-full').count() === 1 && await page.locator('#ddvEditSave').isVisible()
        && await panel.locator('[data-cb-save]').count() === 0);
  check('nothing typed: Save writes nothing', /Nothing/.test(await barSave()) && !puts.length);
  check('every field labelled, its hints and errors described', await labelled(panel));
  check('the template\'s interval is the placeholder', (await chan('ess_setpoint').locator('#dcb_c_ess_setpoint_min_interval_s').getAttribute('placeholder')) === '60');

  // lease without a safe value: refused in the page, with the words
  await ess.locator('#dcb_b_ess_setpoint_lease').fill('90');
  const err = await ess.locator('#dcb_b_ess_setpoint_lease-err').innerText().catch(() => '');
  check('lease without safe: the message at the field', /needs a safe value/i.test(err) && /never fires/i.test(err), err);
  const p0 = puts.length;
  check('lease without safe: Save writes nothing, says so', /Not saved/.test(await barSave()) && puts.length === p0);
  check('lease without safe: the input is invalid and described by the error',
        (await ess.locator('#dcb_b_ess_setpoint_lease').getAttribute('aria-invalid')) === 'true'
        && /dcb_b_ess_setpoint_lease-err/.test(await ess.locator('#dcb_b_ess_setpoint_lease').getAttribute('aria-describedby')));
  await ess.locator('#dcb_b_ess_setpoint_safe_value').fill('0');
  check('with a safe value the error goes', !(await ess.locator('#dcb_b_ess_setpoint_lease-err').count()));
  await shot(ess, 'gx-binding-typed');
  await barSave();
  await page.waitForFunction(() => document.querySelector('.cb-bind[data-cb-name="ess_setpoint"] #dcb_b_ess_setpoint_lease')?.defaultValue === '90');

  // the interval below the template's: warned, with the template's value
  const ch = chan('ess_setpoint');
  await ch.locator('#dcb_c_ess_setpoint_min_interval_s').fill('5');
  const warn = await ch.locator('[data-cb-below]').innerText();
  check('interval below the template: warned with the template\'s 60', /60/.test(warn) && /5/.test(warn), warn);
  check('interval below the template: the input in warning style', await ch.locator('#dcb_c_ess_setpoint_min_interval_s.cb-below-input').count() === 1);
  await ch.locator('summary').click();
  await ch.locator('#dcb_c_ess_setpoint_max_age_s').fill('20');
  await shot(ch, 'gx-channel-below');
  await barSave();
  await page.waitForFunction(() => document.querySelector('#dcb_c_ess_setpoint_min_interval_s')?.defaultValue === '5');

  // round trip: the page reloaded, the API asked
  await reload('gx');
  check('round trip: lease in the form', (await bind('ess_setpoint').locator('#dcb_b_ess_setpoint_lease').inputValue()) === '90');
  check('round trip: safe in the form', (await bind('ess_setpoint').locator('#dcb_b_ess_setpoint_safe_value').inputValue()) === '0');
  check('round trip: interval in the form, still warned', (await chan('ess_setpoint').locator('#dcb_c_ess_setpoint_min_interval_s').inputValue()) === '5'
        && /60/.test(await chan('ess_setpoint').locator('[data-cb-below]').innerText()));
  check('round trip: max age in the form, its Advanced open', (await chan('ess_setpoint').locator('#dcb_c_ess_setpoint_max_age_s').inputValue()) === '20'
        && await chan('ess_setpoint').locator('details.cb-adv[open]').count() === 1);
  let c = await cmdOf('gx', 'ess_setpoint');
  check('round trip: the API lists them', c.lease_s === 90 && JSON.stringify(c.safe) === '{"value":0}' && c.safe_from === 'device'
        && c.min_interval_s === 5 && c.template_min_interval_s === 60 && c.max_age_s === 20, JSON.stringify(c).slice(0, 300));
  const mcc = await cmdOf('gx', 'max_charge_current');
  check('another channel of the same device keeps the template\'s 60', mcc.min_interval_s === 60);

  // off keeps the binding; one Save with another form wrong writes nothing
  // and keeps both (the tick, the typed value)
  await bind('max_charge_current').locator('#dcb_b_max_charge_current_lease').fill('45');
  const tick = panel.locator('.cmd-card[data-cmd="ess_setpoint"] input[type="checkbox"][data-action="toggleDeviceCommand"]');
  await tick.uncheck();
  const p1 = puts.length;
  check('a form wrong: the one Save writes nothing, says so', /Not saved/.test(await barSave()) && puts.length === p1);
  check('…the tick and the typed value stay', !(await tick.isChecked())
        && (await bind('max_charge_current').locator('#dcb_b_max_charge_current_lease').inputValue()) === '45');
  await bind('max_charge_current').locator('#dcb_b_max_charge_current_lease').fill('');
  await barSave();
  c = await cmdOf('gx', 'ess_setpoint');
  check('off: the command is disabled, lease and safe kept', c.enabled === false && c.lease_s === 90 && JSON.stringify(c.safe) === '{"value":0}', JSON.stringify(c).slice(0, 200));
  check('off: the form still shows them', (await bind('ess_setpoint').locator('#dcb_b_ess_setpoint_lease').inputValue()) === '90');
  await panel.locator('.cmd-card[data-cmd="ess_setpoint"] input[type="checkbox"][data-action="toggleDeviceCommand"]').check();
  await barSave();
  c = await cmdOf('gx', 'ess_setpoint');
  check('on again: lease and safe still there', c.enabled === true && c.lease_s === 90 && JSON.stringify(c.safe) === '{"value":0}');

  // a refusal from the server is said at its field
  await page.route('**/api/devices/gx/commands/max_charge_current', r => r.request().method() === 'PUT'
    ? r.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: { errors: ['safe: 7 is refused'], fields: { safe: 'value 7 is refused by the server' } } }) })
    : r.continue());
  const mb = bind('max_charge_current');
  await mb.locator('#dcb_b_max_charge_current_safe_value').fill('7');
  const said422 = await barSave();
  await mb.locator('#dcb_b_max_charge_current_safe_value-err').waitFor();
  check('a 422 field error is shown inline (and the Save says it was not saved)',
        /refused by the server/.test(await mb.locator('#dcb_b_max_charge_current_safe_value-err').innerText()) && /Not saved/.test(said422), said422);
  await page.unroute('**/api/devices/gx/commands/max_charge_current');
  await mb.locator('#dcb_b_max_charge_current_safe_value').fill('');     // left as stored (nothing staged)

  // counters as muted pills (the dispatcher's, injected here)
  await page.route('**/api/devices/gx/commands', async r => {
    const resp = await r.fetch(); const body = await resp.json();
    body.commands.forEach(x => { if (x.name === 'ess_setpoint') x.counters = { superseded: 2, expired: 0, held: 1 }; });
    await r.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await reload('gx');
  const pills = await bind('ess_setpoint').locator('[data-cb-counter]').allInnerTexts();
  check('counters: the ones that happened, as pills', pills.length === 2 && /superseded 2/i.test(pills.join()) && /held 1/i.test(pills.join()), pills.join('|'));
  await page.unroute('**/api/devices/gx/commands');

  // ── the drive: a motor's protections, an alias ──
  await openCommands('vfd1');
  const motor = chan('motor');
  const advIds = await motor.locator('details.cb-adv input').evaluateAll(l => l.map(i => i.dataset.cbField));
  check('motor: Advanced holds max age and what the template declares', advIds.join() === 'max_age_s,min_off_s,max_starts_per_h', advIds.join());
  check('an alias says whose binding it runs, with no fields', /Runs run/.test(await panel.locator('.cmd-card[data-cmd="stop"]').innerText())
        && await panel.locator('.cmd-card[data-cmd="stop"] .cb-bind').count() === 0);
  check('a command with nothing to release has no dead-man', await panel.locator('.cmd-card[data-cmd="reset_fault"] .cb-bind').count() === 0);
  await motor.locator('summary').click();
  await motor.locator('#dcb_c_motor_max_starts_per_h').fill('2.5');
  check('motor: starts per hour must be whole', /whole/.test(await motor.locator('#dcb_c_motor_max_starts_per_h-err').innerText()));
  await shot(motor, 'vfd-motor');
  await motor.locator('#dcb_c_motor_max_starts_per_h').fill('');        // left as stored (nothing staged)

  // ── an installation's unit: read-only, pointing at its group ──
  await openCommands('sunfield-u1');
  const ro = panel.locator('.cb-form');
  check('unit: the fields are shown read-only', await ro.count() > 0 && await panel.locator('.cb-form input:not([disabled])').count() === 0);
  check('unit: no Save of its own', await panel.locator('.cb-form [data-cb-save]').count() === 0);
  check('unit: the pointer to the group', /Set in the installation's group/.test(await panel.locator('[data-cb-pointer]').first().innerText()));
  await shot(chan('power'), 'unit-readonly');
  await panel.locator('[data-cb-pointer] a').first().click();
  const m = page.locator('#endpointModal');
  await m.locator('.cb-grp').waitFor();
  check('unit: the link opens the group editor', await m.locator('#gcb_b_power_limit_lease').count() === 1);

  // ── the group editor: the same fields, saved with the group ──
  check('group: every field labelled', await labelled(m));
  check('group: the template\'s safe as placeholder', (await m.locator('#gcb_b_power_limit_safe_value').getAttribute('placeholder')) === '100');
  await m.locator('#gcb_b_power_limit_safe_value').fill('150');
  await m.locator('[data-endpoint-save]').click(); await page.waitForTimeout(400);
  check('group: a safe value out of bounds is refused at its field', /above the maximum/.test(await m.locator('#gcb_b_power_limit_safe_value-err').innerText().catch(() => '')));
  await m.locator('#gcb_b_power_limit_safe_value').fill('50');
  await m.locator('#gcb_b_power_limit_lease').fill('300');
  await m.locator('#gcb_c_power_min_interval_s').fill('10');
  await shot(m.locator('.cb-grp'), 'group-editor');
  await m.locator('[data-endpoint-save]').click();
  await m.waitFor({ state: 'hidden' });
  const ep = await api('/api/endpoints/sunfield');
  const inv = ep.groups.find(g => g.id === 'inverters');
  const pl = inv.commands.find(x => x.name === 'power_limit');
  check('group: binding stored with the group', pl.binding.lease_s === 300 && JSON.stringify(pl.binding.safe) === '{"value":50,"revert_s":0}' && pl.binding.enabled === true, JSON.stringify(pl.binding));
  check('group: channel override stored with the group', JSON.stringify(inv.channels) === '{"power":{"min_interval_s":10}}', JSON.stringify(inv.channels));
  const u1 = await cmdOf('sunfield-u1', 'power_limit');
  check('group: every unit runs them', u1.lease_s === 300 && u1.safe?.value === 50 && u1.min_interval_s === 10);
  await page.locator(`[data-group="inverters"] button[data-action="openGroupModal"]`).click();
  await m.locator('.cb-grp').waitFor();
  check('group: reopened, the editor shows them', (await m.locator('#gcb_b_power_limit_lease').inputValue()) === '300'
        && (await m.locator('#gcb_b_power_limit_safe_value').inputValue()) === '50'
        && (await m.locator('#gcb_c_power_min_interval_s').inputValue()) === '10');
  await m.locator('.modal-close').click();
  await openCommands('sunfield-u1');
  check('group: the unit page shows them read-only', (await panel.locator('#dcb_b_power_limit_lease').inputValue()) === '300'
        && (await panel.locator('#dcb_c_power_min_interval_s').inputValue()) === '10');

  // ── the status page: a command lane falling behind ──
  await page.route('**/api/status', async r => {
    const resp = await r.fetch(); const body = await resp.json();
    body.command_lanes = [{ lane: 'mqtt:gx/default', pending: 3, running: true, oldest_s: 17.2, max_age_s: 30 }];
    await r.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  await page.click('[data-page="status"]');
  const item = page.locator('.st-issue[data-key="lane:mqtt:gx/default"]');
  await item.first().waitFor({ timeout: 15000 });
  const said = await item.first().innerText();
  check('status: the late lane is an attention item, named after its device', /Victron GX/.test(said) && /3 command/.test(said) && /17 s of its 30 s/.test(said), said.replace(/\s+/g, ' '));
  await page.unroute('**/api/status');

  check('no page errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 400)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
