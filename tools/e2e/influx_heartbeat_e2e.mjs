/* InfluxDB heartbeat (ADR-0029): the value is a visible setting, never a
 * constant only the code knows.
 *  - Settings → InfluxDB shows the value stored in config.yaml (600, written
 *    into the fixture's file on its first load — the fixture has no key), with
 *    a help text that says what it does, the trade-off and "0 = off"; a new
 *    value and 0 are saved and shown again (0 is shown, never a blank);
 *  - the device's Outputs → InfluxDB → Publishing has the same field, with
 *    the Settings label: empty = the gateway's, and "gateway: N s"
 *    beside it always says what that is; the device's own value is saved
 *    (the Edit device modal, staged: its one Save),
 *    a fraction refused, blank goes back to the gateway's;
 *  - the help text in Romanian.
 *
 *   tools/e2e/fixture_up.sh drive 18097        (MBG_FX_NAME=mbg-fx-influx-hb)
 *   MBG_URL=http://127.0.0.1:18097 CHROMIUM_PATH=<chrome> node influx_heartbeat_e2e.mjs
 *
 * Config it expects: login off, `vfd1`, InfluxDB without heartbeat_interval
 * in the fixture file. Settings are put back to 600 at the end.
 */
import { chromium } from 'playwright';
import { openDeviceTab, saveDeviceEditor, editorSettled } from './lib/device-page.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18097';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const errs = [];
const influx = async () => (await (await fetch(BASE + '/api/config/influxdb')).json());
const pub = async () => (await (await fetch(BASE + '/api/devices')).json()).devices.find(d => d.id === 'vfd1').publishing;

async function settings(page) {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(() => app.openSettingsTab('influxdb')); await page.waitForTimeout(1200);
}
async function outputs(page) {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(() => app.openDeviceDetail('vfd1')); await page.waitForTimeout(1800);
  await openDeviceTab(page, 'outputs'); await page.waitForTimeout(600);
  await page.click('#outTab-influxdb'); await page.waitForTimeout(400);
}
const saveInflux = async page => {
  await page.click('[data-action="saveAndApply"][data-args=\'["influx"]\']'); await page.waitForTimeout(1500);
};

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422|50[23]/.test(m.text())) errs.push(m.text()); });
  page.on('pageerror', e => errs.push(String(e)));

  // ── Settings → InfluxDB ──
  check('the file got its starting value', (await influx()).heartbeat_interval === 600);
  await settings(page);
  const hb = page.locator('#cfgInfluxHeartbeat');
  check('Settings show the stored value, not a blank', await hb.inputValue() === '600');
  check('…labelled', /Rewrite an unchanged value every/.test(await page.locator('label[for="cfgInfluxHeartbeat"]').innerText()));
  const hint = await page.locator('#cfgInfluxHeartbeatHint').innerText();
  check('the help text says what it does, the trade-off and 0 = off',
        /last value/.test(hint) && /more points/.test(hint) && /emptier/.test(hint) && /0 = off/.test(hint)
        && /blank field keeps the stored value/.test(hint), hint);
  check('the field points at its help', await hb.getAttribute('aria-describedby') === 'cfgInfluxHeartbeatHint');

  await hb.fill('300'); await saveInflux(page);
  check('a new value is saved', (await influx()).heartbeat_interval === 300);
  await settings(page);
  check('…and shown again', await page.locator('#cfgInfluxHeartbeat').inputValue() === '300');
  await page.locator('#cfgInfluxHeartbeat').fill('0'); await saveInflux(page);
  await settings(page);
  check('0 (off) is saved and shown as 0', (await influx()).heartbeat_interval === 0
        && await page.locator('#cfgInfluxHeartbeat').inputValue() === '0');
  await page.locator('#cfgInfluxHeartbeat').fill('300'); await saveInflux(page);

  // ── the device's Outputs → InfluxDB → Publishing ──
  await outputs(page);
  const dhb = page.locator('#pubHb-influxdb');
  check('the device has the field, empty = the gateway\'s', await dhb.count() === 1 && await dhb.inputValue() === '');
  check('…with the Settings label', /Rewrite an unchanged value every/.test(await page.locator('label[for="pubHb-influxdb"]').innerText()));
  check('"0 = never rewrite" is the heartbeat\'s, not the write interval\'s',
        /0 = never rewrite/.test(await page.locator('#pubHbZero-influxdb').innerText())
        && !/0 = never resend/.test(await page.locator('#outDetail').innerText()));
  check('"gateway: 300 s" beside it', (await page.locator('#pubHbGw-influxdb').innerText()).trim() === 'gateway: 300 s');
  check('Now says the rewrite', /rewritten every 300 s/.test(await page.locator('#pubNow-influxdb').innerText()));
  // (the Edit device modal, L3.4: staged, saved with its one Save)
  await dhb.fill('120');
  await saveDeviceEditor(page);
  let p = await pub();
  check('the device\'s own value is saved', p.own.influxdb.heartbeat_interval === 120 && p.effective.influxdb.heartbeat_interval === 120);
  await outputs(page);
  check('…and Now says it', /rewritten every 120 s/.test(await page.locator('#pubNow-influxdb').innerText())
        && (await page.locator('#pubHbGw-influxdb').innerText()).trim() === 'gateway: 300 s');
  await page.locator('#pubHb-influxdb').fill('1.5');
  await editorSettled(page);
  check('a fraction of a second is refused before Save, at its field', (await page.getAttribute('#pubHb-influxdb', 'aria-invalid')) === 'true'
        && (await page.getAttribute('#devEditSave', 'aria-disabled')) === 'true' && (await pub()).own.influxdb.heartbeat_interval === 120);
  await page.locator('#pubHb-influxdb').fill('');
  await saveDeviceEditor(page);
  await outputs(page);
  p = await pub();
  check('blank goes back to the gateway\'s', p.own.influxdb.heartbeat_interval === null && p.effective.influxdb.heartbeat_interval === 300
        && await page.locator('#pubHb-influxdb').inputValue() === '');

  // ── Romanian ──
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await settings(page);
  const ro = await page.locator('#cfgInfluxHeartbeatHint').innerText();
  check('the help text in Romanian', /„ultima valoare”/.test(ro) && /0 = oprit/.test(ro), ro);
  check('the label in Romanian', /Rescrie o valoare neschimbată/.test(await page.locator('label[for="cfgInfluxHeartbeat"]').innerText()));
  await outputs(page);
  check('"gateway: N s" in Romanian too', (await page.locator('#pubHbGw-influxdb').innerText()).trim() === 'gateway: 300 s'
        && /rescrisă la 300 s/.test(await page.locator('#pubNow-influxdb').innerText()));
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  await ctx.close();

  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  await fetch(BASE + '/api/config/influxdb', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                               body: JSON.stringify({ heartbeat_interval: 600 }) }).catch(() => {});
  const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0);
}
