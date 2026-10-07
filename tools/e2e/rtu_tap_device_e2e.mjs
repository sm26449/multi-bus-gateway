/* End-to-end validation of a listen-only RTU tap as a DEVICE (3.85.0): created
 * through the Add Device wizard, hearing a live bus, edited in the device
 * workspace, and the serial-line rules surfacing in the wizard.
 *
 * Needs an EPHEMERAL instance (it creates and deletes devices) with auth off,
 * and a fake pack bus on a PTY inside it — the master asks unit 3 for the
 * Seplos PIA block (FC4 0x1000 x18, 53.13 V + unit id in centivolts / -2.7 A) once a second:
 *
 *   docker cp tools/e2e/tap_feeder.py <ctr>:/tmp/
 *   docker exec -d -u <app uid> <ctr> python /tmp/tap_feeder.py   # the APP user,
 *     or the tap cannot open the PTY (the gateway runs as uid 10001)
 *   cd tools/e2e && MBG_URL=http://127.0.0.1:8099 TAP_PORT=/tmp/ttyTAPE2E node rtu_tap_device_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const PORT = process.env.TAP_PORT || '/tmp/ttyTAPE2E';
const ID = 'tap-e2e';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const device = async id => (await api('/api/devices')).body.devices.find(d => d.id === id);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// leftovers from an aborted run
for (const id of [ID, 'tap-e2e-bad', 'rtutcp-e2e']) await api(`/api/devices/${id}`, { method: 'DELETE' });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
// step 4's refusal is a deliberate 422 — the browser logs it as a resource error
page.on('console', m => { if (m.type() === 'error' && !/status of 422/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

async function wizardToTap({ serial, baud, unit }) {
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#deviceWizardModal input[name="devWizProto"][value="rtu"]', { state: 'attached' });
  await page.click('#deviceWizardModal label:has(input[name="devWizProto"][value="rtu"])');
  await page.click('#deviceWizardModal label:has(input[name="devWizRtuMode"][value="rtu_tap"])');
  await page.fill('#devWizSerial', serial);
  await page.selectOption('#devWizBaud', String(baud));
  await page.fill('#devWizUnitR', String(unit));
}

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(800);

  // ---- 1. wizard: the tap is a mode of Modbus RTU --------------------------
  await wizardToTap({ serial: PORT, baud: 9600, unit: 3 });
  check('tap mode selected', await page.isChecked('input[name="devWizRtuMode"][value="rtu_tap"]'));
  check('serial fields shown', await page.isVisible('#devWizSerial'));
  check('no Test button in tap mode',
    !(await page.locator('#devWizRtuDirect [data-action="devWizardTest"]').count()));
  check('listen-only note shown', await page.isVisible('#devWizRtuDirect .bi-ear'));
  // switching away and back keeps the typed port
  await page.click('label:has(input[name="devWizRtuMode"][value="rtu"])');
  check('direct mode brings the Test button back',
    await page.locator('#devWizRtuDirect [data-action="devWizardTest"]').count() === 1);
  await page.click('label:has(input[name="devWizRtuMode"][value="rtu_tap"])');
  check('serial port survives a mode switch', await page.inputValue('#devWizSerial') === PORT);

  await page.click('#devWizNext');
  await page.click('.tpl-pick[data-tpl="seplos_bms_v3_rtu_tap"]');
  await page.click('#devWizNext');
  await page.fill('#devWizId', ID);
  await page.fill('#devWizName', 'Tap E2E');
  if (!(await page.isChecked('#devWizEnabled'))) await page.click('label:has(#devWizEnabled)');
  await page.click('#devWizNext');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' }).catch(() => {});

  let dev = await device(ID);
  check('device created as rtu_tap', dev?.protocol === 'rtu_tap', dev?.protocol);
  check('serial block echoed', dev?.serial?.serial_port === PORT && +dev?.serial?.baudrate === 9600,
    JSON.stringify(dev?.serial));
  check('unit id saved', dev?.connection?.unit_id === 3);

  // ---- 2. it hears the bus -----------------------------------------------
  let t = {};
  for (let i = 0; i < 10 && !t.ok; i++) { await sleep(1000); t = (await api(`/api/devices/${ID}/test`, { method: 'POST' })).body; }
  check('Test reports hearing unit 3', t.ok === true && /hearing unit 3/.test(t.message), t.message);
  const vals = (await api(`/api/values?device=${ID}`)).body.values || {};
  const byName = Object.fromEntries(Object.values(vals).map(v => [v.name, v.value]));
  // the feeder offsets each pack's voltage by its unit id: 5313 + 3
  check('pack_voltage decoded 53.16', byName.pack_voltage === 53.16, String(byName.pack_voltage));
  check('current decoded -2.7', byName.current === -2.7, String(byName.current));
  check('soc decoded 76.9', byName.soc === 76.9, String(byName.soc));

  // ---- 3. device workspace: Edit tab knows the tap --------------------------
  await page.evaluate(id => window.app.openDeviceDetail(id), ID);
  await page.waitForSelector('[data-dtab="edit"]');
  await page.click('[data-dtab="edit"]');
  check('edit radio is the tap', await page.isChecked('input[name="ddvProto"][value="rtu_tap"]'));
  check('serial block visible', await page.isVisible('#ddvRtu'));
  check('tcp block hidden', !(await page.isVisible('#ddvTcp')));
  check('serial prefilled', await page.inputValue('#ddvSerial') === PORT);
  await page.click('[data-action="deviceDetailTest"]');
  await page.waitForFunction(() => document.getElementById('ddvTestResult')?.textContent.trim());
  const tr = await page.textContent('#ddvTestResult');
  check('detail Test asks the saved device', /hearing unit 3/.test(tr), tr);

  // save unchanged: the client restarts and re-joins the shared reader
  await page.locator('[data-action="saveDeviceDetail"]:visible').first().click();
  await page.waitForTimeout(1500);
  dev = await device(ID);
  check('save keeps rtu_tap', dev?.protocol === 'rtu_tap', dev?.protocol);
  t = {};
  for (let i = 0; i < 8 && !t.ok; i++) { await sleep(1000); t = (await api(`/api/devices/${ID}/test`, { method: 'POST' })).body; }
  check('still hearing after the restart', t.ok === true, t.message);

  // ---- 4. the line rules surface in the wizard ----------------------------
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(500);
  await wizardToTap({ serial: PORT, baud: 19200, unit: 4 });
  await page.click('#devWizNext');
  await page.click('.tpl-pick[data-tpl="seplos_bms_v3_rtu_tap"]');
  await page.click('#devWizNext');
  await page.fill('#devWizId', 'tap-e2e-bad');
  await page.click('#devWizNext');
  await page.waitForFunction(() => document.getElementById('devWizFeedback')?.textContent.trim());
  const fb = await page.textContent('#devWizFeedback');
  check('other baud on a tapped line refused, in the wizard', /same line settings/.test(fb), fb);
  check('refused device not created', !(await device('tap-e2e-bad')));
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));

  // ---- 5. regression: rtu-tcp host edits stick ----------------------------
  await api('/api/devices', { method: 'POST', body: JSON.stringify({
    id: 'rtutcp-e2e', enabled: false,
    connection: { protocol: 'rtu-tcp', host: '127.0.0.1', port: 9, unit_id: 1 } }) });
  await page.evaluate(id => window.app.openDeviceDetail(id), 'rtutcp-e2e');
  await page.waitForSelector('[data-dtab="edit"]');
  await page.click('[data-dtab="edit"]');
  await page.fill('#ddvHost', '127.0.0.2');
  await page.locator('[data-action="saveDeviceDetail"]:visible').first().click();
  await page.waitForTimeout(1200);
  const rt = await device('rtutcp-e2e');
  check('rtu-tcp host edit persisted', rt?.connection?.host === '127.0.0.2' && rt?.protocol === 'rtu-tcp',
    `${rt?.protocol} ${rt?.connection?.host}`);

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 300));
} finally {
  for (const id of [ID, 'tap-e2e-bad', 'rtutcp-e2e']) await api(`/api/devices/${id}`, { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
