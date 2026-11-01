/* Device → Outputs, as a list and the chosen output's settings — in the Edit
 * device modal (⋯ → Outputs…, L3.4):
 *  - every output on the left with its state and summary, the open one on
 *    the right; the arrow keys move along the list; the choice is remembered;
 *  - every switch and setting is staged: nothing is sent until the modal's
 *    one Save, which sends them with the rest (a rename) in one PATCH; an
 *    output going off is reviewed first; a REST push without a URL is
 *    refused at its field before Save;
 *  - publishing (the device's own rhythm, or Settings') is staged too; the
 *    Settings link leaves the modal (asking about unsaved edits);
 *  - what is not an output lives elsewhere: the write lock with the
 *    commands, nothing of the PQ recorder in Outputs (no recorder here);
 *  - the gateway's own device and an endpoint unit are edited in the modal
 *    too (L3.11b): the primary's MQTT/InfluxDB and the gateway's HA switch
 *    say why they are held, with the way to Settings; a unit's outputs are
 *    its installation's (said, with the way there), its MQTT/InfluxDB locked;
 *  - a phone shows the list as a row; the Romanian page.
 *
 *   MBG_URL=http://127.0.0.1:18090 CHROMIUM_PATH=<chrome> node device_outputs_e2e.mjs
 *
 * Config it expects: login off, `vfd1` (a device with a command), nothing
 * else switched since start. An installation `pv` with one unit is made here
 * when the fixture has none (disabled, a TEST-NET address) and removed after.
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab, editorSection, editorSettled, saveDeviceEditor, closeDeviceEditor } from './lib/device-page.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const errs = [];
const dev = async (ctx, id) => (await (await ctx.request.get(BASE + '/api/devices')).json()).devices.find(d => d.id === id);
const OUT = '#devEditPanel-outputs';

async function open(page, id, tab = 'outputs') {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(i => app.openDeviceDetail(i), id); await page.waitForTimeout(1800);
  await openDeviceTab(page, tab); await page.waitForTimeout(900);
}

const madePv = !(await (await fetch(BASE + '/api/endpoints/pv')).ok)
  && (await fetch(BASE + '/api/endpoints', { method: 'POST', headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify({ id: 'pv', name: 'PV', template: 'fronius_sunspec_inverter', enabled: false,
                              connection: { protocol: 'tcp', host: '192.0.2.40', port: 502 }, units: [1] }) })).ok;
try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422|Failed to fetch/.test(m.text())) errs.push(m.text()); });   // (a poll cut short by page.goto: not the page's error)
  page.on('pageerror', e => errs.push(String(e)));
  const dlg = await watchDialogs(page);
  await page.addInitScript(() => {                    // a clean start, once (not on every load)
    try { if (!sessionStorage.getItem('e2e-fresh')) { localStorage.removeItem('mbg-output'); sessionStorage.setItem('e2e-fresh', '1'); } } catch (e) {}
  });

  await open(page, 'vfd1');
  check('Outputs… opens the Edit modal at Outputs', await page.locator('#devEditModal.active').count() === 1
        && await page.getAttribute('#devEditModal .dev-shell-tab[aria-selected="true"]', 'data-shell-sec') === 'outputs');
  const items = page.locator('.outputs-nav .outputs-nav-item');
  const names = (await items.allInnerTexts()).map(x => x.split('\n')[0].trim());
  check('every output is listed', JSON.stringify(names) === JSON.stringify(['MQTT', 'InfluxDB', 'HTTP / JSON output', 'OPC UA', 'REST push']), names);
  check('each with its state', await page.locator('.outputs-nav .o-state').count() === 5);
  check('and where it goes (the topic prefix)', /mbg\/devices\/vfd1/.test(await items.first().innerText()));
  check('the first is open, as a tab', await page.locator('#outTab-mqtt').getAttribute('aria-selected') === 'true'
        && /MQTT/.test(await page.locator('#outDetail h3').innerText()));
  check('one Save: the modal\'s — no Save button of an output\'s own', await page.locator(`${OUT} button:has-text("Save")`).count() === 0
        && await page.locator(`${OUT} [data-action="savePublishing"], ${OUT} [data-action="saveRestPush"]`).count() === 0);
  // L3.3: the write lock is not an output (a header pill, ⋯ → Lock/Unlock writes…);
  // a device with no PQ recorder shows no PQ card among its outputs
  check('the write lock is not an output', await page.locator(`${OUT} #ddvWriteLock, ${OUT} #ddvMenuLock`).count() === 0
        && await page.locator(`${OUT} #devEditPq, ${OUT} [data-draft-path^="pq."]`).count() === 0);

  await page.locator('#outTab-mqtt').focus();
  await page.keyboard.press('ArrowDown'); await page.waitForTimeout(300);
  check('the arrow keys move along the list', await page.locator('#outTab-influxdb').getAttribute('aria-selected') === 'true'
        && await page.evaluate(() => document.activeElement?.id) === 'outTab-influxdb');
  await page.keyboard.press('End'); await page.waitForTimeout(300);
  check('End goes to the last', await page.locator('#outTab-rest').getAttribute('aria-selected') === 'true');

  // MQTT off, HA off, a rename: staged, then one Save (reviewed: an output goes off)
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  await page.locator('#outEnabled').uncheck(); await page.waitForTimeout(300);
  check('MQTT off is staged: nothing sent yet', (await dev(ctx, 'vfd1')).mqtt_enabled === true);
  check('…and the list says it is off', /off/i.test(await page.locator('#outTab-mqtt .o-state').innerText()));
  await page.locator('#ddvHaDisc').uncheck();
  await editorSection(page, 'identity');
  await page.fill('#ddvName', 'Pump drive (renamed)');
  await editorSettled(page);
  check('the footer counts them: 3 changes, applied live', /3 changes · applies live/.test(await page.locator('#devEditSummary').innerText()),
        await page.locator('#devEditSummary').innerText());
  const nReview = dlg.seen.length;
  const r1 = await saveDeviceEditor(page);
  const sent1 = r1 && r1.request().postDataJSON();
  check('MQTT off is reviewed before Save', dlg.seen.length === nReview + 1 && /Review/.test(dlg.seen[nReview].title)
        && dlg.seen[nReview].details.some(x => /mqtt\.enabled/.test(x)), dlg.seen[nReview]);
  check('one PATCH: the name and both switches', r1 && r1.ok() && sent1.sections.identity.name === 'Pump drive (renamed)'
        && sent1.sections.outputs.mqtt.enabled === false && sent1.sections.outputs.ha_discovery === false, sent1);
  const after = await dev(ctx, 'vfd1');
  check('stored together', after.name === 'Pump drive (renamed)' && after.mqtt_enabled === false
        && after.ha_discovery_enabled === false, { mqtt: after.mqtt_enabled, ha: after.ha_discovery_enabled });

  // the chosen output is remembered
  await open(page, 'vfd1');
  await page.click('#outTab-http'); await page.waitForTimeout(300);
  await open(page, 'vfd1');
  check('the open output is remembered', await page.locator('#outTab-http').getAttribute('aria-selected') === 'true');
  await page.locator('#outEnabled').check(); await page.waitForTimeout(300);
  check('HTTP on, staged: its URL said "once saved"', /once saved/.test(await page.locator('#outDetail').innerText())
        && (await dev(ctx, 'vfd1')).http_output_enabled !== true);
  const r2 = await saveDeviceEditor(page);
  check('HTTP on saved without a review (an output going on)', r2 && r2.ok() && (await dev(ctx, 'vfd1')).http_output_enabled === true);
  await open(page, 'vfd1');
  check('…and then shows its feed', await page.locator('#ddvHttpUrl').count() === 1 && await page.locator('#ddvHttpStats').count() === 1);

  // REST push with no URL: refused at its field before Save
  await page.click('#outTab-rest'); await page.waitForTimeout(300);
  await page.locator('#outEnabled').check(); await page.waitForTimeout(300);
  await editorSettled(page);
  check('REST push without a URL: refused before Save, at its field', /problem/.test(await page.locator('#devEditSummary').innerText())
        && (await page.getAttribute('#devEditSave', 'aria-disabled')) === 'true'
        && /rest_push\.url/.test(await page.locator('#devEditErrors').innerText())
        && (await page.getAttribute('#ddvRestUrl', 'aria-invalid')) === 'true', await page.locator('#devEditErrors').innerText());
  await page.locator('#outEnabled').uncheck();
  await editorSettled(page);
  check('…switched back: the refusal goes', !(await page.locator('#devEditErrors li').count()));
  check('…nothing was stored', !(await dev(ctx, 'vfd1')).rest_push_enabled);

  // publishing: the device's own rhythm, or the gateway's — staged
  const pub = async id => (await dev(ctx, id)).publishing;
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('MQTT says how it publishes now, from Settings', /Now:\s*on change/.test(await page.locator('#outDetail').innerText())
        && await page.locator('#pubMode-mqtt').inputValue() === '');
  await page.selectOption('#pubMode-mqtt', 'all');
  await saveDeviceEditor(page);
  await open(page, 'vfd1');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('its own: at every reading', (await pub('vfd1')).own.mqtt.publish_mode === 'all'
        && /Now:\s*every reading/.test(await page.locator('#outDetail').innerText()));
  await page.selectOption('#pubMode-mqtt', '');
  await page.fill('#pubSecs-mqtt', '0');
  await saveDeviceEditor(page);
  await open(page, 'vfd1');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  let pv = await pub('vfd1');
  check('back to Settings for the mode, never resent', pv.own.mqtt.publish_mode === null && pv.own.mqtt.heartbeat_interval === 0
        && /on change only/.test(await page.locator('#outDetail').innerText()));
  await page.fill('#pubSecs-mqtt', '-4');
  await editorSettled(page);
  check('a negative number of seconds is refused before Save', /problem/.test(await page.locator('#devEditSummary').innerText())
        && (await pub('vfd1')).own.mqtt.heartbeat_interval === 0);
  await page.fill('#pubSecs-mqtt', '0');
  await page.click('#outTab-influxdb'); await page.waitForTimeout(300);
  await page.fill('#pubSecs-influxdb', '30');
  await saveDeviceEditor(page);
  await open(page, 'vfd1');
  await page.click('#outTab-influxdb'); await page.waitForTimeout(300);
  check('InfluxDB: at most one point every 30 s', (await pub('vfd1')).effective.influxdb.write_interval === 30
        && /30 s/.test(await page.locator('#outDetail').innerText()));
  await page.locator('#outDetail a[data-action="openSettingsTab"]').first().click(); await page.waitForTimeout(1200);
  check('the Settings link leaves the modal for the InfluxDB settings', await page.locator('#cfgInfluxPublishMode').isVisible()
        && await page.locator('#devEditModal.active').count() === 0);

  await open(page, 'umg512');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('the gateway\'s own device: the Edit device modal (L3.11b)', await page.locator('#devEditModal.active').count() === 1
        && await page.locator(`${OUT} .outputs-nav`).isVisible());
  check('…it follows Settings, said, not editable', await page.locator('#pubMode-mqtt').count() === 0
        && /follows/.test(await page.locator('#outDetail').innerText()));
  check('…its MQTT switch held, saying why, with the way to Settings → MQTT',
        (await page.getAttribute('#outEnabled', 'aria-disabled')) === 'true' && await page.locator('#outDetail [data-lock-way="mqtt"]').count() >= 1);
  check('…the gateway\'s HA switch held, with the way to Settings → MQTT',
        (await page.getAttribute('#ddvHaDisc', 'aria-disabled')) === 'true' && await page.locator('#ddvHaDiscLock [data-lock-way="mqtt"]').count() === 1);
  await closeDeviceEditor(page);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  await page.evaluate(() => app.openDeviceDetail('vfd1')); await page.waitForTimeout(1500);

  // the write lock: a pill in the header, switched from ⋯ (L3.3: no card on Commands)
  check('the write lock is a header pill and a ⋯ item, not on the Commands tab',
        await page.locator('#ddvFacts .dev-sum-lock').isVisible() && await page.locator('#ddvActionsMenu #ddvMenuLock').count() === 1
        && await page.locator('[data-dpanel="commands"] #ddvWriteLock').count() === 0);

  // an endpoint unit: its installation's outputs, in the modal (staged, L3.11b)
  await open(page, 'pv-u1');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('a unit\'s Outputs… is the Edit device modal', await page.locator('#devEditModal.active').count() === 1);
  check('a unit\'s MQTT is locked to its installation', await page.locator('#outEnabled').isDisabled()
        && /installation|endpoint/i.test(await page.locator('#outDetail').innerText()));
  check('a unit\'s Outputs offer the installation', await page.locator('#devEditPanel-outputs [data-de-way="installation"]').count() >= 1);
  await closeDeviceEditor(page);

  // Romanian
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await open(page, 'vfd1');
  const ro = await page.locator(OUT).innerText();
  check('in Romanian', /oprit|activ|serviciu/i.test(ro) && !/devices\.sink|devices\.outputs|devedit\./.test(ro), ro.slice(0, 160));
  check('the list is named for screen readers', !!(await page.locator('.outputs-nav').getAttribute('aria-label')));
  await closeDeviceEditor(page);
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  await ctx.close();

  // a phone: the list is a row, the open one in view, nothing wider than the screen
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const pp = await phone.newPage();
  pp.on('pageerror', e => errs.push(String(e)));
  await pp.addInitScript(() => { try { localStorage.setItem('mbg-output', 'rest'); } catch (e) {} });
  await open(pp, 'vfd1');
  const nav = await pp.locator('.outputs-nav').boundingBox();
  const act = await pp.locator('.outputs-nav-item.active').boundingBox();
  check('on a phone the list is one row', nav && nav.height < 140, nav);
  check('…with the open output in view', act && act.x >= 0 && act.x + act.width <= 390 + 1, act);
  check('…and no sideways page scroll', await pp.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  await phone.close();

  check('no native dialog', dlg.native === 0);
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); if (madePv) await fetch(BASE + '/api/endpoints/pv', { method: 'DELETE' }); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
