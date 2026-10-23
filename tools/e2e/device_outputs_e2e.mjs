/* Device → Outputs, as a list and the chosen output's settings:
 *  - every output on the left with its state and summary, the open one on
 *    the right; the arrow keys move along the list; the choice is remembered;
 *  - every switch applies at once (MQTT and InfluxDB too, no Save & Apply);
 *    an edit from the Edit tab keeps them; a REST push without a URL is
 *    refused and its switch goes back;
 *  - what is not an output lives elsewhere: the write lock with the
 *    commands, nothing of the PQ recorder in Outputs;
 *  - an endpoint unit's MQTT/InfluxDB are locked to its installation;
 *  - a phone shows the list as a row; the Romanian page.
 *
 *   MBG_URL=http://127.0.0.1:18090 CHROMIUM_PATH=<chrome> node device_outputs_e2e.mjs
 *
 * Config it expects: login off, `vfd1` (a device with a command), nothing
 * else switched since start. An installation `pv` with one unit is made here
 * when the fixture has none (disabled, a TEST-NET address) and removed after.
 */
import { chromium } from 'playwright';
import { openDeviceTab } from './lib/device-page.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const errs = [];
const dev = async (ctx, id) => (await (await ctx.request.get(BASE + '/api/devices')).json()).devices.find(d => d.id === id);

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
  page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422/.test(m.text())) errs.push(m.text()); });
  page.on('pageerror', e => errs.push(String(e)));
  await page.addInitScript(() => {                    // a clean start, once (not on every load)
    try { if (!sessionStorage.getItem('e2e-fresh')) { localStorage.removeItem('mbg-output'); sessionStorage.setItem('e2e-fresh', '1'); } } catch (e) {}
  });

  await open(page, 'vfd1');
  const items = page.locator('.outputs-nav .outputs-nav-item');
  const names = (await items.allInnerTexts()).map(x => x.split('\n')[0].trim());
  check('every output is listed', JSON.stringify(names) === JSON.stringify(['MQTT', 'InfluxDB', 'HTTP / JSON output', 'OPC UA', 'REST push']), names);
  check('each with its state', await page.locator('.outputs-nav .o-state').count() === 5);
  check('and where it goes (the topic prefix)', /mbg\/devices\/vfd1/.test(await items.first().innerText()));
  check('the first is open, as a tab', await page.locator('#outTab-mqtt').getAttribute('aria-selected') === 'true'
        && /MQTT/.test(await page.locator('#outDetail h3').innerText()));
  check('no Save & Apply in Outputs', await page.locator('[data-dpanel="outputs"] button:has-text("Save & Apply")').count() === 0);
  check('the write lock is not an output', await page.locator('[data-dpanel="outputs"] #ddvWriteLock').count() === 0
        && await page.locator('[data-dpanel="outputs"] #ddvPqEnabled').count() === 0);

  await page.locator('#outTab-mqtt').focus();
  await page.keyboard.press('ArrowDown'); await page.waitForTimeout(300);
  check('the arrow keys move along the list', await page.locator('#outTab-influxdb').getAttribute('aria-selected') === 'true'
        && await page.evaluate(() => document.activeElement?.id) === 'outTab-influxdb');
  await page.keyboard.press('End'); await page.waitForTimeout(300);
  check('End goes to the last', await page.locator('#outTab-rest').getAttribute('aria-selected') === 'true');

  // MQTT: off at once, no restart, kept by an edit
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  await page.locator('#outEnabled').uncheck(); await page.waitForTimeout(1200);
  check('MQTT switches off at once', (await dev(ctx, 'vfd1')).mqtt_enabled === false);
  check('…and the list says so', /off/i.test(await page.locator('#outTab-mqtt .o-state').innerText()));
  await page.locator('#ddvHaDisc').uncheck(); await page.waitForTimeout(1000);
  check('Home Assistant discovery switches at once', (await dev(ctx, 'vfd1')).ha_discovery_enabled === false);

  await openDeviceTab(page, 'connection'); await page.waitForTimeout(800);
  await page.fill('#ddvName', 'Pump drive (renamed)');
  await page.click('[data-dpanel="connection"] button[data-action="saveDeviceDetail"]'); await page.waitForTimeout(2500);
  const after = await dev(ctx, 'vfd1');
  check('an edit keeps the outputs as switched', after.name === 'Pump drive (renamed)' && after.mqtt_enabled === false
        && after.ha_discovery_enabled === false, { mqtt: after.mqtt_enabled, ha: after.ha_discovery_enabled });

  // the chosen output is remembered
  await open(page, 'vfd1');
  await page.click('#outTab-http'); await page.waitForTimeout(300);
  await open(page, 'vfd1');
  check('the open output is remembered', await page.locator('#outTab-http').getAttribute('aria-selected') === 'true');
  await page.locator('#outEnabled').check(); await page.waitForTimeout(1500);
  check('HTTP on shows its feed', (await dev(ctx, 'vfd1')).http_output_enabled === true
        && await page.locator('#ddvHttpUrl').count() === 1 && await page.locator('#ddvHttpStats').count() === 1);

  // REST push with no URL: refused, the switch goes back
  await page.click('#outTab-rest'); await page.waitForTimeout(300);
  await page.locator('#outEnabled').check(); await page.waitForTimeout(1200);
  check('REST push without a URL is refused and its switch goes back', !(await page.locator('#outEnabled').isChecked())
        && !(await dev(ctx, 'vfd1')).rest_push_enabled);

  // publishing: the device's own rhythm, or the gateway's
  const pub = async id => (await dev(ctx, id)).publishing;
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('MQTT says how it publishes now, from Settings', /Now:\s*on change/.test(await page.locator('#outDetail').innerText())
        && await page.locator('#pubMode-mqtt').inputValue() === '');
  await page.selectOption('#pubMode-mqtt', 'all');
  await page.click('button[data-action="savePublishing"]'); await page.waitForTimeout(1000);
  check('its own: at every reading', (await pub('vfd1')).own.mqtt.publish_mode === 'all'
        && /Now:\s*every reading/.test(await page.locator('#outDetail').innerText()));
  await page.selectOption('#pubMode-mqtt', '');
  await page.fill('#pubSecs-mqtt', '0');
  await page.click('button[data-action="savePublishing"]'); await page.waitForTimeout(1000);
  let pv = await pub('vfd1');
  check('back to Settings for the mode, never resent', pv.own.mqtt.publish_mode === null && pv.own.mqtt.heartbeat_interval === 0
        && /on change only/.test(await page.locator('#outDetail').innerText()));
  await page.fill('#pubSecs-mqtt', '-4');
  await page.click('button[data-action="savePublishing"]'); await page.waitForTimeout(600);
  check('a negative number of seconds is refused', (await pub('vfd1')).own.mqtt.heartbeat_interval === 0);
  await page.click('#outTab-influxdb'); await page.waitForTimeout(300);
  await page.fill('#pubSecs-influxdb', '30');
  await page.click('button[data-action="savePublishing"]'); await page.waitForTimeout(1000);
  check('InfluxDB: at most one point every 30 s', (await pub('vfd1')).effective.influxdb.write_interval === 30
        && /30 s/.test(await page.locator('#outDetail').innerText()));
  await page.locator('#outDetail a[data-action="openSettingsTab"]').first().click(); await page.waitForTimeout(1200);
  check('the Settings link opens the InfluxDB settings', await page.locator('#cfgInfluxPublishMode').isVisible());

  await open(page, 'umg512');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('the gateway\'s own device follows Settings, said, not editable', await page.locator('#pubMode-mqtt').count() === 0
        && /follows/.test(await page.locator('#outDetail').innerText()));
  await open(page, 'vfd1');

  // the write lock is with the commands
  await openDeviceTab(page, 'commands'); await page.waitForTimeout(800);
  check('the write lock is on the Commands tab', await page.locator('[data-dpanel="commands"] #ddvWriteLock').count() === 1);

  // an endpoint unit: MQTT and InfluxDB belong to its installation
  await open(page, 'pv-u1');
  await page.click('#outTab-mqtt'); await page.waitForTimeout(300);
  check('a unit\'s MQTT is locked to its installation', await page.locator('#outEnabled').isDisabled()
        && /installation|endpoint/i.test(await page.locator('#outDetail').innerText()));
  check('a unit\'s Outputs offer the installation', await page.locator('[data-dpanel="outputs"] button:has-text("Open installation")').count() === 1);

  // Romanian
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await open(page, 'vfd1');
  const ro = await page.locator('[data-dpanel="outputs"]').innerText();
  check('in Romanian', /oprit|activ|serviciu/i.test(ro) && !/devices\.sink|devices\.outputs/.test(ro), ro.slice(0, 160));
  check('the list is named for screen readers', !!(await page.locator('.outputs-nav').getAttribute('aria-label')));
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

  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); if (madePv) await fetch(BASE + '/api/endpoints/pv', { method: 'DELETE' }); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
