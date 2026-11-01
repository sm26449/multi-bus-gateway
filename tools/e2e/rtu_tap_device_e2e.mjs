/* End-to-end validation of a listen-only RTU tap as a DEVICE (3.85.0): created
 * through Add Device (L3.9: a model that only listens → its Connection step,
 * Listen-only pre-filled with its reason), hearing a live bus, edited in the
 * device workspace, and the serial-line rules surfacing in Add (a second
 * device on the tapped line inherits its line, locked).
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
import { openDeviceTab, saveDeviceEditor, editorSettled } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';
import { openAdd, pickModel, answer, fill, next, waitCheck } from './lib/add-device.mjs';

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
const dlg = await watchDialogs(page);        // a review before Save (a bus move): answered yes
const errs = [];
// step 4's refusal is a deliberate 422 — the browser logs it as a resource error
page.on('console', m => { if (m.type() === 'error' && !/status of 422/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

// Add Device: the listening model, then its Connection step on this host's port
async function wizardToTap({ serial, baud, unit }) {
  await openAdd(page);
  await pickModel(page, 'seplos', 'seplos_bms_v3_rtu_tap');
  await answer(page, 'Reach', 'here').catch(() => {});
  await page.waitForSelector('#csAddSerial');
  await page.fill('#csAddSerial', serial);
  await page.locator('#csAddSerial').blur();
  await page.waitForTimeout(500);
  if (baud && !(await page.getAttribute('#csAddBaud', 'aria-disabled'))) await page.selectOption('#csAddBaud', String(baud));
  await fill(page, 'Unit', unit);
}

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(800);

  // ---- 1. Add: a model that only listens → Listen-only, said why ------------
  await wizardToTap({ serial: PORT, baud: 9600, unit: 3 });
  check('Listen-only pre-filled by the model, its reason said', await page.isChecked('#csAddRole-listen')
    && /listen/i.test(await page.locator('#csAddRoleQ').innerText()));
  check('Master cannot be taken with this model (aria-disabled, its reason beside)',
    (await page.getAttribute('#csAddRole-poll', 'aria-disabled')) === 'true');
  check('serial fields shown', await page.isVisible('#csAddSerial'));
  check('no Test button for a tap (it never transmits)', !(await page.locator('#csAddTestBtn').count()));
  // the role answered again keeps the typed port
  await page.locator('label:has(#csAddRole-listen)').click();
  check('serial port survives an answer', await page.inputValue('#csAddSerial') === PORT);

  await next(page);                                    // → Check (it listens: heard n of m)
  const heard = await waitCheck(page, 30000);
  check('the check listens on the bus (heard n of m groups)', /heard \d+ of \d+/.test(heard.heard) || heard.refused, JSON.stringify(heard).slice(0, 300));
  await next(page);                                    // → Name & save
  await page.fill('#adId', ID);
  await page.fill('#adName', 'Tap E2E');
  await page.locator('#adId').dispatchEvent('input');
  if (!(await page.isChecked('#adEnabled'))) await page.click('label:has(#adEnabled)');
  await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 15000 }).catch(() => {});
  check('the review says: writes none — this connection only listens', /none/.test(await page.locator('#adReviewBody [data-layer="writes"]').innerText().catch(() => '')));
  await page.click('#addDevNext');
  await page.waitForSelector('#adResult', { timeout: 15000 }).catch(() => {});
  await page.click('#adResDone').catch(() => {});

  let dev = await device(ID);
  check('device created as rtu_tap', dev?.protocol === 'rtu_tap', dev?.protocol);
  check('serial block echoed', dev?.serial?.serial_port === PORT && +dev?.serial?.baudrate === 9600,
    JSON.stringify(dev?.serial));
  check('unit id saved', dev?.connection?.unit_id === 3);

  // ---- 2. it hears the bus -----------------------------------------------
  let t = {};
  for (let i = 0; i < 10 && !t.ok; i++) { await sleep(1000); t = (await api(`/api/devices/${ID}/test`, { method: 'POST' })).body; }
  check('Test reports hearing unit 3', t.ok === true && /hearing unit 3/.test(t.message), t.message);
  // "hearing" comes with the first window; the one carrying these may be next
  let byName = {};
  for (let i = 0; i < 20 && byName.pack_voltage == null; i++) {
    const vals = (await api(`/api/values?device=${ID}`)).body.values || {};
    byName = Object.fromEntries(Object.values(vals).map(v => [v.name, v.value]));
    if (byName.pack_voltage == null) await sleep(500);
  }
  // the feeder offsets each pack's voltage by its unit id: 5313 + 3
  check('pack_voltage decoded 53.16', byName.pack_voltage === 53.16, String(byName.pack_voltage));
  check('current decoded -2.7', byName.current === -2.7, String(byName.current));
  check('soc decoded 76.9', byName.soc === 76.9, String(byName.soc));

  // ---- 3. device workspace: Edit tab knows the tap --------------------------
  await page.evaluate(id => window.app.openDeviceDetail(id), ID);
  await openDeviceTab(page, 'connection');
  check('edit shows the tap (role Listen-only)', await page.isChecked('input[name="ddvRole"][value="listen"]'));
  check('serial block visible', await page.isVisible('#ddvSerialPick'));
  check('tcp block hidden', !(await page.isVisible('#ddvHost')));
  check('serial prefilled', await page.inputValue('#ddvSerial') === PORT);
  await page.click('[data-action="deviceDetailTest"]');
  await page.waitForFunction(() => document.getElementById('ddvTestResult')?.textContent.trim());
  const tr = await page.textContent('#ddvTestResult');
  check('detail Test asks the saved device', /hearing unit 3/.test(tr), tr);

  // save unchanged (the Edit modal holds it back: "No changes"); the device
  // keeps running and hearing the shared reader
  await editorSettled(page);
  await page.click('#devEditSave', { force: true });
  await page.click('#devEditCancel');
  await page.waitForTimeout(1500);
  dev = await device(ID);
  check('save keeps rtu_tap', dev?.protocol === 'rtu_tap', dev?.protocol);
  t = {};
  for (let i = 0; i < 8 && !t.ok; i++) { await sleep(1000); t = (await api(`/api/devices/${ID}/test`, { method: 'POST' })).body; }
  check('still hearing after the restart', t.ok === true, t.message);

  // ---- 4. the line rules surface in the wizard ----------------------------
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(500);
  await wizardToTap({ serial: PORT, baud: 0, unit: 4 });
  const lineWhy = await page.locator('#csAddLineWhy').innerText();
  check('a second device on the tapped line inherits its line, locked, saying whose',
    (await page.getAttribute('#csAddBaud', 'aria-disabled')) === 'true' && await page.inputValue('#csAddBaud') === '9600'
    && /tap-e2e|Tap E2E/.test(lineWhy), lineWhy);
  await page.selectOption('#csAddBaud', '19200').catch(() => {});
  await page.waitForTimeout(300);
  check('…another baud cannot be chosen there', await page.inputValue('#csAddBaud') === '9600');
  check('refused device not created', !(await device('tap-e2e-bad')));
  await page.evaluate(() => { window.app._ad.done = true; window.app.closeModal('addDeviceModal'); });

  // ---- 5. regression: rtu-tcp host edits stick ----------------------------
  await api('/api/devices', { method: 'POST', body: JSON.stringify({
    id: 'rtutcp-e2e', enabled: false,
    connection: { protocol: 'rtu-tcp', host: '127.0.0.1', port: 9, unit_id: 1 } }) });
  await page.evaluate(id => window.app.openDeviceDetail(id), 'rtutcp-e2e');
  await openDeviceTab(page, 'connection');
  await page.fill('#ddvHost', '127.0.0.2');      // RTU over the network, its typed address
  const nRev = dlg.seen.length;
  await saveDeviceEditor(page);                     // a converter's address is its bus: reviewed, answered yes
  check('rtu-tcp host edit: reviewed first (a bus move), named', dlg.seen.length > nRev && /Review/.test(dlg.seen[nRev].title)
    && (dlg.seen[nRev].details || []).some(x => /host/.test(x)), JSON.stringify(dlg.seen[nRev] || null));
  await page.waitForTimeout(600);
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
