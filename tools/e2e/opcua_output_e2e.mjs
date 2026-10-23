/* Device → Outputs → OPC UA: a device from before the choice existed is
 * served with every value; a new one is off; "only the values I choose"
 * lists what the device reads, keeps the choice across a reload, and the
 * OPC UA server serves exactly that (tools/e2e/opcua_client_check.py-style
 * browse is done by the caller); the Romanian page.
 *
 *   docker run -d --rm --name mbg-opcout-test -p 127.0.0.1:18090:8080 -p 127.0.0.1:14840:4840 \
 *     -e UI_HOST=0.0.0.0 --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app <image> main.py
 *   MBG_URL=http://127.0.0.1:18090 CHROMIUM_PATH=<chrome> node opcua_output_e2e.mjs
 *
 * Config it expects: login off, an old device `vfd1` (no opcua: block). The
 * OPC UA server is switched on here when it is off, and back off after.
 */
import { chromium } from 'playwright';
import { openDeviceTab } from './lib/device-page.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(20000);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
const sink = async id => (await (await ctx.request.get(BASE + '/api/devices')).json()).devices.find(d => d.id === id)?.sinks?.opcua || {};

async function openOutputs(id) {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]'); await page.waitForTimeout(1200);
  await page.locator(`button[data-action="openDeviceDetail"][data-args*='"${id}"']`).first().click();
  await page.waitForTimeout(1800);
  await openDeviceTab(page, 'outputs');
  await page.waitForTimeout(800);
  await page.click('#outTab-opcua'); await page.waitForTimeout(500);
}

const opcuaWas = await (await fetch(BASE + '/api/opcua')).json();
const opcuaPut = body => fetch(BASE + '/api/opcua', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({ ...opcuaWas, ...body }) });
if (!opcuaWas.enabled) await opcuaPut({ enabled: true, encryption: 'optional' });
try {
  const made = await ctx.request.post(BASE + '/api/devices', { data: {
    id: 'pump2', name: 'Second pump', template: 'delta_vfd_el', enabled: true,
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 1, unit_id: 2 } } });
  check('a new device is made', made.ok(), made.ok() ? '' : await made.text());

  await openOutputs('vfd1');
  const card = page.locator('#outDetail');
  check('the Outputs tab lists OPC UA and opens it', /OPC UA/.test(await card.innerText())
        && await page.locator('#outTab-opcua').getAttribute('aria-selected') === 'true');
  check('a device from before the choice is served', await page.locator('#outEnabled').isChecked());
  check('every value, by default', await page.locator('input[name="ddvOpcuaMode"][value="all"]').isChecked());
  check('the card names the endpoint', /opc\.tcp:\/\/.+:4840\/mbg/.test(await card.innerText()), (await card.innerText()).slice(0, 200));

  await openOutputs('pump2');
  check('a new device is not served until turned on', !(await page.locator('#outEnabled').isChecked())
        && (await sink('pump2')).enabled === false);
  check('…and its choices are hidden while off', await page.locator('input[name="ddvOpcuaMode"]').count() === 0);
  await page.locator('#outEnabled').check(); await page.waitForTimeout(1000);
  check('turned on from the switch, at once', (await sink('pump2')).enabled === true);

  await page.locator('input[name="ddvOpcuaMode"][value="chosen"]').check(); await page.waitForTimeout(1500);
  const boxes = page.locator('#ddvOpcuaList input[type=checkbox]');
  const n = await boxes.count();
  check('"only the values I choose" lists what the device reads', n > 3, `${n} values`);
  await page.locator('#ddvOpcuaFilter').fill('frequency'); await page.waitForTimeout(300);
  const shown = await page.locator('#ddvOpcuaList .opcua-pick:visible').count();
  check('the filter narrows the list', shown > 0 && shown < n, `${shown}/${n}`);
  await page.locator('#ddvOpcuaList .opcua-pick:visible input').first().check();
  await page.locator('#ddvOpcuaFilter').fill(''); await page.waitForTimeout(200);
  const second = page.locator('#ddvOpcuaList input[type=checkbox]:not(:checked)').first();
  const secondName = await second.getAttribute('value');
  await second.check();
  check('the count follows the ticks', /2/.test(await page.locator('#ddvOpcuaCount').innerText()));
  await page.locator('button[data-action="saveOpcuaFields"]').click(); await page.waitForTimeout(1000);
  const fields = (await sink('pump2')).fields || [];
  check('the chosen values are saved', fields.length === 2 && fields.includes(secondName), fields);

  await openOutputs('pump2');
  check('after a reload the choice is shown', await page.locator('input[name="ddvOpcuaMode"][value="chosen"]').isChecked()
        && await page.locator('#ddvOpcuaList input[type=checkbox]:checked').count() === 2);

  await page.locator('input[name="ddvOpcuaMode"][value="all"]').check(); await page.waitForTimeout(1000);
  check('back to every value, at once', ((await sink('pump2')).fields || []).length === 0);
  await page.locator('input[name="ddvOpcuaMode"][value="chosen"]').check(); await page.waitForTimeout(1200);
  await page.locator('#ddvOpcuaList input[type=checkbox]').first().check();
  await page.locator('button[data-action="saveOpcuaFields"]').click(); await page.waitForTimeout(800);
  console.log('SERVED_FIELD=' + ((await sink('pump2')).fields || [])[0]);

  await openOutputs('vfd1');
  await page.locator('#outEnabled').uncheck(); await page.waitForTimeout(1000);
  check('an old device turned off', (await sink('vfd1')).enabled === false);

  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await openOutputs('pump2');
  const ro = await page.locator('#outDetail').innerText();
  check('in Romanian', /Doar valorile pe care le aleg/.test(ro) && /Publică acest echipament/.test(ro) && !/devices\.sink/.test(ro), ro.slice(0, 200));
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); if (!opcuaWas.enabled) await opcuaPut({ enabled: false }); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
