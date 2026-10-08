/* A NEW USER, start to finish: register maps written from a spec (CSV import,
 * YAML import, the visual editor), one device over every protocol added with
 * the Add Device wizard, EXACT decoded values checked against simulators that
 * hold known numbers, the word-order mistake found and fixed from Diagnostics
 * → Probe without a restart, a virtual meter re-serving a value on Modbus TCP
 * (plus its export/import), and the rules import/export smoke.
 *
 * Needs an EPHEMERAL instance with auth off, run as a container this script
 * can `docker exec` into (CTR, default mbg-ui-sandbox) whose app user is uid
 * 10001, on the docker network pv-stack-network. The script itself:
 *   - starts a THROWAWAY anonymous broker `mbg-e2e-mqtt` on that network (and
 *     stops it at the end) — never the production broker;
 *   - copies sims/new_user_sims.py into the container and runs it as uid 10001
 *     (Modbus TCP :15020, Modbus RTU on a PTY /tmp/ttySIM, HTTP JSON :18089,
 *     an MQTT publisher) — see that file for the exact register map;
 *   - creates templates e2e_nu_*, devices e2e-nu-*, virtual meter e2e_nu_vm*,
 *     and removes them again at the start (idempotent) and at the end.
 *
 *   cd tools/e2e && CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:8099 node new_user_e2e.mjs
 */
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const NET = process.env.DOCKER_NET || 'pv-stack-network';
const BROKER = 'mbg-e2e-mqtt';
const APP_UID = process.env.APP_UID || '10001';
const HERE = path.dirname(fileURLToPath(import.meta.url));

const T = { tcp: 'e2e_nu_tcp', rtu: 'e2e_nu_rtu', http: 'e2e_nu_http', mqtt: 'e2e_nu_mqtt' };
const D = { tcp: 'e2e-nu-tcp', rtu: 'e2e-nu-rtu', http: 'e2e-nu-http', mqtt: 'e2e-nu-mqtt' };
const VM = 'e2e_nu_vm', VM_COPY = 'e2e_nu_vm_copy';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + String(extra).replace(/\s+/g, ' ').slice(0, 240) : ''}`);
};
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const text = await r.text();
  let body; try { body = JSON.parse(text); } catch { body = text; }
  return { status: r.status, body };
};
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });
const del = p => api(p, { method: 'DELETE' });
// what a new user trips over without the product being wrong in a testable
// way — printed, and summarised at the end (not counted as failures)
const notes = [];
const note = msg => { notes.push(msg); console.log(`NOTE ${msg}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const docker = (args, opts = {}) => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...opts });
const inCtr = (cmd, user = APP_UID) => docker(['exec', '-u', user, CTR, 'sh', '-c', cmd]);
const near = (a, b, eps = 1e-6) => typeof a === 'number' && Math.abs(a - b) <= eps;

/** name → value of a device's live store */
async function values(dev) {
  const v = (await api(`/api/values?device=${dev}`)).body.values || {};
  return Object.fromEntries(Object.values(v).map(x => [x.name, x.value]));
}
/** poll until pred(values) holds (or time runs out); returns the last values */
async function waitValues(dev, pred, secs = 20) {
  let v = {};
  for (let i = 0; i < secs * 2; i++) {
    v = await values(dev);
    if (pred(v)) return v;
    await sleep(500);
  }
  return v;
}

// ── setup / teardown (idempotent) ────────────────────────────────────────
async function cleanup() {
  await del(`/api/virtual-meters/${VM}`);
  for (const t of [VM, VM_COPY]) await del(`/api/virtual-meters/template/${t}`);
  for (const id of Object.values(D)) await del(`/api/devices/${id}`);
  for (const id of Object.values(T)) await del(`/api/device-templates/${id}`);
  // a deleted device keeps its measurement file on purpose (re-adding the same
  // id reuses it when the names still fit) — a test must start from nothing
  try { inCtr('rm -rf /app/config/devices/e2e-nu-*'); } catch { /* none */ }
}
function stopSims() {
  try { inCtr('touch /tmp/nu_sims.stop'); } catch { /* not running */ }
  try { docker(['stop', BROKER]); } catch { /* not running */ }
}

stopSims();
await cleanup();
await sleep(800);
docker(['run', '-d', '--rm', '--name', BROKER, '--network', NET, 'eclipse-mosquitto:2',
        'mosquitto', '-c', '/mosquitto-no-auth.conf']);
docker(['cp', path.join(HERE, 'sims', 'new_user_sims.py'), `${CTR}:/tmp/new_user_sims.py`]);
inCtr('rm -f /tmp/nu_sims.stop');
docker(['exec', '-d', '-u', APP_UID, '-e', `MQTT_HOST=${BROKER}`, CTR, 'sh', '-c',
        'python3 /tmp/new_user_sims.py > /tmp/nu_sims.log 2>&1']);
// the HTTP device must be on the LAN: the gateway refuses loopback for HTTP
// (SSRF guard), so the sim is reached on the container's own network address
const CTR_IP = docker(['inspect', '-f', `{{(index .NetworkSettings.Networks "${NET}").IPAddress}}`, CTR]).trim();
await sleep(2500);

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
page.on('dialog', d => d.accept());
const errs = [];
// a refused save is a deliberate 4xx the browser logs as a resource error
page.on('console', m => { if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

const setCell = async (loc, v) => { await loc.fill(String(v)); await loc.dispatchEvent('change'); };
const lastRow = () => page.locator('#devTplBody tbody tr').last();
async function toTemplates() {
  await page.click('[data-page="templates"]');
  await page.waitForSelector('#tmImportCsvBtn');
}
async function importMap({ fmt, id, name, vendor, transport = '', order, text }) {
  await toTemplates();
  await page.click(fmt === 'yaml' ? '#tmImportYamlBtn' : '#tmImportCsvBtn');
  await page.waitForSelector('#csvImportModal.active');
  await page.fill('#csvId', id);
  await page.fill('#csvName', name);
  await page.fill('#csvVendor', vendor);
  await page.selectOption('#csvTransport', transport);
  await page.selectOption('#csvByteOrder', order);
  await page.fill('#csvText', text);
  await page.click('#csvImportModal [data-action="csvPreview"]');
  await page.waitForFunction(() => document.getElementById('csvPreviewResult')?.textContent.trim());
  return (await page.textContent('#csvPreviewResult')).trim();
}
async function closeToasts() {
  await page.evaluate(() => document.querySelectorAll('.toast').forEach(t => t.remove()));
}
const tpl = async id => (await api(`/api/device-templates/${id}`)).body.device_template || {};

async function addDevice({ proto, fill, template, id, name }) {
  await page.click('[data-page="devices"]');
  if (!(await page.isVisible('#devicesListView'))) {
    // the wizard jumped into the last device's Measurements, and the Devices
    // tab keeps that view — only its back arrow returns to the list
    if (!notes.some(n => n.startsWith('Devices tab')))
      note('Devices tab: after Add Device the measurements editor opens, and clicking "Devices" again stays there — only the ← arrow returns to the list');
    await page.click('#deviceRegistersView [data-action="closeDeviceRegisters"]');
  }
  await page.locator('button[data-action="openDeviceWizard"]:visible').first().click();
  await page.waitForSelector('#deviceWizardModal input[name="devWizProto"][value="tcp"]', { state: 'attached' });
  await page.click(`#deviceWizardModal label:has(input[name="devWizProto"][value="${proto}"])`);
  const testOut = await fill();
  await page.click('#devWizNext');
  await page.click(`.tpl-pick[data-tpl="${template}"]`);
  await page.click('#devWizNext');
  await page.fill('#devWizId', id);
  await page.fill('#devWizName', name);
  if (!(await page.isChecked('#devWizEnabled'))) await page.click('label:has(#devWizEnabled)');
  await page.click('#devWizNext');
  return testOut;
}
async function wizardTest(resultId) {
  await page.locator('#deviceWizardModal [data-action="devWizardTest"]:visible').first().click();
  await page.waitForFunction(id => document.getElementById(id)?.textContent.trim(), resultId, { timeout: 20000 });
  return (await page.textContent(`#${resultId}`)).trim();
}

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ═══ 1. Templates from a register spec ═══════════════════════════════════
  // 1a. TCP map from CSV — imported as ABCD, which is WRONG for this device
  //     (the manual's "word order" was missed); section 4 finds and fixes it.
  const tcpCsv = [
    'address,name,label,unit,type,scale,fc',
    '0,voltage_l1_n,Voltage L1-N,V,float,1,fc3',
    '2,energy_counter,Energy counter,Wh,int32,1,fc3',
    '4,power_active_total,Active power,W,int16,10,fc3',
    '5,power_reactive_total,Reactive power,var,int16,10,fc3',
    '8,relay_state,Relay,,uint16,1,coil',
    '9,door_contact,Door contact,,uint16,1,DI',
    '10,ir_counter,Input register counter,,uint16,1,fc4',
  ].join('\n');
  let pv = await importMap({ fmt: 'csv', id: T.tcp, name: 'E2E TCP meter', vendor: 'E2E', order: 'big', text: tcpCsv });
  check('CSV preview: 7 measurements parsed', /^7\s/.test(pv), pv.slice(0, 120));
  check('CSV preview: the fc column is recognised', /register_type/.test(pv), pv.slice(0, 160));
  await page.click('#csvImportBtn');
  await page.waitForSelector('#csvImportModal', { state: 'hidden' });
  let t = await tpl(T.tcp);
  const rt = Object.fromEntries((t.registers || []).map(r => [r.name, r.register_type || 'holding']));
  check('CSV import saved the TCP map', (t.registers || []).length === 7, `${(t.registers || []).length} rows`);
  check('CSV import: FC1 / FC2 / FC4 rows typed', rt.relay_state === 'coil' && rt.door_contact === 'discrete'
    && rt.ir_counter === 'input' && rt.voltage_l1_n === 'holding', JSON.stringify(rt));
  check('CSV import: word order from the form (ABCD)', t.protocol?.byte_order === 'big', JSON.stringify(t.protocol));

  // 1b. HR 0 and IR 0 are two registers (3.91): one map holds both; only a
  //     repeat within ONE table is refused, before import
  const bothYaml = 'registers:\n  - {address: 0, name: voltage_l1_n, data_type: float}\n'
    + '  - {address: 0, name: frequency, data_type: uint16, register_type: input}\n';
  pv = await importMap({ fmt: 'yaml', id: 'e2e_nu_clash', name: 'clash', vendor: 'E2E', order: 'big', text: bothYaml });
  check('YAML preview accepts HR 0 + IR 0 in one map (one row per address per table)',
    !/duplicate address/.test(pv) && !(await page.isDisabled('#csvImportBtn')), pv.slice(0, 160));
  await page.evaluate(() => window.app.closeModal('csvImportModal'));
  const clashYaml = 'registers:\n  - {address: 0, name: voltage_l1_n, data_type: float}\n'
    + '  - {address: 0, name: frequency, data_type: uint16}\n';
  pv = await importMap({ fmt: 'yaml', id: 'e2e_nu_clash', name: 'clash', vendor: 'E2E', order: 'big', text: clashYaml });
  check('YAML preview refuses two rows at HR 0',
    /duplicate address 0 in the holding table/.test(pv) && await page.isDisabled('#csvImportBtn'), pv.slice(0, 160));
  await page.evaluate(() => window.app.closeModal('csvImportModal'));

  // 1c. RTU map from YAML (the file's own byte_order header)
  const rtuYaml = [
    'byte_order: big',
    'registers:',
    '  - {address: 0, name: voltage_l1_n, label: Voltage L1-N, unit: V, data_type: float}',
    '  - {address: 2, name: counter_a, label: Counter A, data_type: uint16}',
    '  - {address: 20, name: frequency, label: Frequency, unit: Hz, data_type: uint16, scale: 100, register_type: input}',
  ].join('\n');
  pv = await importMap({ fmt: 'yaml', id: T.rtu, name: 'E2E RTU meter', vendor: 'E2E', order: 'big', text: rtuYaml });
  check('YAML preview: 3 measurements parsed', /^3\s/.test(pv), pv.slice(0, 120));
  await page.click('#csvImportBtn');
  await page.waitForSelector('#csvImportModal', { state: 'hidden' });
  t = await tpl(T.rtu);
  check('YAML import saved the RTU map', (t.registers || []).length === 3, `${(t.registers || []).length} rows`);

  // 1d. visual editor: add a coil row (FC1) to the RTU map
  await toTemplates();
  await page.click(`[data-tm-edit="${T.rtu}"]`);
  await page.waitForSelector('#devTplModal.active');
  check('editor shows the imported order (ABCD)', await page.inputValue('#tplByteOrder') === 'big');
  check('editor offers FC1/FC2 per row', (await page.locator('#devTplBody tbody tr').first()
    .locator('select[data-f="register_type"] option').allInnerTexts()).join(',') === 'FC3,FC4,FC1,FC2');
  if (!(await page.locator('#devTplBody .tpl-pg-iv').count()))
    note('editor: an imported map whose rows name no poll group shows NO interval (they read every 5 s on "normal", invisible here)');
  await page.click('#devTplBody [data-action="tplAddRow"]');
  await setCell(lastRow().locator('[data-f="address"]'), 30);
  await setCell(lastRow().locator('[data-f="name"]'), 'relay_state');
  await setCell(lastRow().locator('[data-f="label"]'), 'Relay');
  await setCell(lastRow().locator('[data-f="data_type"]'), 'uint16');
  await lastRow().locator('select[data-f="register_type"]').selectOption('coil');
  await page.click('#devTplSaveBtn');
  await page.waitForSelector('#devTplModal', { state: 'hidden' });
  t = await tpl(T.rtu);
  const relay = (t.registers || []).find(r => r.name === 'relay_state');
  check('editor saved a coil row (FC1) at 30', relay?.register_type === 'coil' && relay?.address === 30, JSON.stringify(relay));

  // 1e. HTTP map in the visual editor
  async function newMap({ id, name, transport, rows, interval }) {
    await toTemplates();
    await page.click('#tmNewBtn');
    await page.waitForSelector('#devTplModal.active');
    await page.fill('#tplId', id);
    await page.fill('#tplName', name);
    await page.uncheck('#devTplBody .tpl-tr[value="tcp"]');
    await page.check(`#devTplBody .tpl-tr[value="${transport}"]`);
    const cols = {
      path: await page.locator('#devTplBody tbody [data-f="json_path"]').count(),
      topic: await page.locator('#devTplBody tbody [data-f="topic"]').count(),
      fc: await page.locator('#devTplBody tbody [data-f="register_type"]').count() };
    if (interval) {
      const iv = page.locator('#devTplBody .tpl-pg-iv[data-pg="normal"]');
      await iv.fill(String(interval)); await iv.dispatchEvent('change');
    }
    for (let i = 0; i < rows.length; i++) {
      if (i) await page.click('#devTplBody [data-action="tplAddRow"]');
      const row = page.locator('#devTplBody tbody tr').nth(i);
      for (const [f, v] of Object.entries(rows[i])) await setCell(row.locator(`[data-f="${f}"]`), v);
    }
    await page.click('#devTplSaveBtn');
    await page.waitForSelector('#devTplModal', { state: 'hidden' }).catch(() => {});
    const err = await page.locator('#tplErrors').textContent().catch(() => '');
    if (await page.isVisible('#devTplModal')) await page.evaluate(() => window.app.closeModal('devTplModal'));
    return { cols, err };
  }
  let r = await newMap({ id: T.http, name: 'E2E HTTP inverter', transport: 'http', interval: 1, rows: [
    { address: 1, name: 'power_active_total', unit: 'W', json_path: 'Body.Data.PAC' },
    { address: 2, name: 'voltage_l1_n', unit: 'V', json_path: 'Body.Data.UAC' },
    { address: 3, name: 'string_energy', unit: 'kWh', json_path: 'Body.Data.Strings[0].E' },
  ] });
  check('HTTP ticked: json_path column shown, FC column gone', r.cols.path > 0 && r.cols.fc === 0, JSON.stringify(r.cols));
  t = await tpl(T.http);
  check('editor saved the HTTP map', (t.registers || []).length === 3 && t.protocol?.transports?.join() === 'http',
    r.err || JSON.stringify(t.protocol));
  check('poll interval from the editor saved', t.poll_groups?.normal?.interval === 1, JSON.stringify(t.poll_groups));
  const tlist = (await api('/api/device-templates')).body.templates || [];
  check('HTTP map classed as HTTP', tlist.find(x => x.id === T.http)?.transport === 'http',
    tlist.find(x => x.id === T.http)?.transport);

  // 1f. MQTT map in the visual editor (two fields of one JSON + a bare-number topic)
  r = await newMap({ id: T.mqtt, name: 'E2E MQTT meter', transport: 'mqtt', rows: [
    { address: 1, name: 'power_active_total', unit: 'W', topic: '~/state', json_path: 'p' },
    { address: 2, name: 'soc', unit: '%', topic: '~/state', json_path: 'soc' },
    { address: 3, name: 'temperature', unit: '°C', topic: '~/temp' },
  ] });
  check('MQTT ticked: topic + json_path columns shown', r.cols.path > 0 && r.cols.topic > 0, JSON.stringify(r.cols));
  t = await tpl(T.mqtt);
  check('editor saved the MQTT map', (t.registers || []).length === 3 && t.protocol?.transports?.join() === 'mqtt',
    r.err || JSON.stringify(t.protocol));

  // ═══ 2. Devices through the Add Device wizard ═══════════════════════════
  await closeToasts();
  const tcpTest = await addDevice({ proto: 'tcp', template: T.tcp, id: D.tcp, name: 'E2E TCP meter', fill: async () => {
    await page.fill('#devWizHost', '127.0.0.1');
    await page.fill('#devWizPort', '15020');
    await page.fill('#devWizUnit', '1');
    return wizardTest('devWizTestResult');
  } });
  check('wizard Test (TCP) succeeds', /^✓/.test(tcpTest), tcpTest);
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });

  const rtuTest = await addDevice({ proto: 'rtu', template: T.rtu, id: D.rtu, name: 'E2E RTU meter', fill: async () => {
    await page.click('#deviceWizardModal label:has(input[name="devWizRtuMode"][value="rtu"])');
    await page.fill('#devWizSerial', '/tmp/ttySIM');
    await page.selectOption('#devWizBaud', '9600');
    await page.fill('#devWizUnitR', '7');
    return wizardTest('devWizTestResult2');
  } });
  check('wizard Test (RTU direct serial) succeeds', /^✓/.test(rtuTest), rtuTest);
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });

  // HTTP: a new user's first try is the sim on loopback — refused (SSRF guard)
  const loopTest = await addDevice({ proto: 'http', template: T.http, id: D.http, name: 'E2E HTTP inverter', fill: async () => {
    await page.fill('#devWizUrl', 'http://127.0.0.1:18089/status.json');
    return wizardTest('devWizTestResult3');
  } });
  check('wizard Test refuses a loopback HTTP URL', /^✗/.test(loopTest), loopTest);
  await page.waitForFunction(() => document.getElementById('devWizFeedback')?.textContent.trim());
  const fb = await page.textContent('#devWizFeedback');
  check('…and Save says why (private LAN / allow_nonlan)', /private LAN|allow_nonlan/.test(fb), fb);
  // back to step 1, the LAN address of the same sim
  await page.click('#devWizBack'); await page.click('#devWizBack');
  await page.fill('#devWizUrl', `http://${CTR_IP}:18089/status.json`);
  const httpTest = await wizardTest('devWizTestResult3');
  check('wizard Test (HTTP, LAN address) succeeds', /^✓/.test(httpTest), httpTest);
  await page.click('#devWizNext'); await page.click('#devWizNext'); await page.click('#devWizNext');
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });

  const mqttTest = await addDevice({ proto: 'mqtt', template: T.mqtt, id: D.mqtt, name: 'E2E MQTT meter', fill: async () => {
    await page.fill('#devWizBroker', BROKER);
    await page.fill('#devWizMqttTopic', 'e2e/meter/#');
    return wizardTest('devWizTestResult4');
  } });
  check('wizard Test (MQTT) succeeds', /^✓/.test(mqttTest), mqttTest);
  await page.waitForSelector('#deviceWizardModal', { state: 'hidden' });

  const devs = (await api('/api/devices')).body.devices || [];
  for (const [k, id] of Object.entries(D)) {
    const d = devs.find(x => x.id === id);
    check(`device ${id} created (${k})`, d && d.template === T[k] && d.enabled, d ? `${d.protocol} ${d.template}` : 'missing');
  }

  // ═══ 3. Exact values ═════════════════════════════════════════════════════
  let v = await waitValues(D.tcp, x => x.power_active_total !== undefined && x.ir_counter !== undefined);
  check('TCP int16 scaled: 12345 / 10 = 1234.5 W', near(v.power_active_total, 1234.5), String(v.power_active_total));
  check('TCP int16 signed: -1234 / 10 = -123.4', near(v.power_reactive_total, -123.4), String(v.power_reactive_total));
  check('TCP FC4 input register 4321', v.ir_counter === 4321, String(v.ir_counter));
  check('TCP FC1 coil 8 ON', v.relay_state === 1 || v.relay_state === true, String(v.relay_state));
  check('TCP FC2 discrete input 9 ON', v.door_contact === 1 || v.door_contact === true, String(v.door_contact));
  check('TCP float with the WRONG order (ABCD) is not 230.5', v.voltage_l1_n !== undefined && !near(v.voltage_l1_n, 230.5, 0.01),
    String(v.voltage_l1_n));
  check('TCP int32 with the WRONG order is not 123456789', v.energy_counter !== 123456789, String(v.energy_counter));

  v = await waitValues(D.rtu, x => x.frequency !== undefined && x.relay_state !== undefined);
  check('RTU float ABCD 231.25 V', near(v.voltage_l1_n, 231.25), String(v.voltage_l1_n));
  check('RTU uint16 777', v.counter_a === 777, String(v.counter_a));
  check('RTU FC4 5001 / 100 = 50.01 Hz', near(v.frequency, 50.01), String(v.frequency));
  check('RTU FC1 coil 30 ON', v.relay_state === 1 || v.relay_state === true, String(v.relay_state));

  v = await waitValues(D.http, x => x.string_energy !== undefined);
  check('HTTP Body.Data.PAC = 1500', v.power_active_total === 1500, String(v.power_active_total));
  check('HTTP Body.Data.UAC = 231.2', near(v.voltage_l1_n, 231.2), String(v.voltage_l1_n));
  check('HTTP array path Strings[0].E = 42.5', near(v.string_energy, 42.5), String(v.string_energy));

  v = await waitValues(D.mqtt, x => x.temperature !== undefined && x.soc !== undefined);
  check('MQTT json_path p = 800', v.power_active_total === 800, String(v.power_active_total));
  check('MQTT json_path soc = 55.5', near(v.soc, 55.5), String(v.soc));
  check('MQTT bare-number topic ~/temp = 21.5', near(v.temperature, 21.5), String(v.temperature));

  // ═══ 4. Word order: find it in Diagnostics → Probe, fix it there ════════
  await page.click('[data-page="diagnostics"]');
  await page.waitForSelector('#probeBtn');
  await page.waitForFunction(id => [...document.querySelectorAll('#probeDevice option')].some(o => o.value === id), D.tcp);
  await page.selectOption('#probeDevice', D.tcp);
  await page.fill('#probeAddr', '0');
  await page.selectOption('#probeType', 'holding');
  await page.selectOption('#probeCount', '2');
  await page.click('#probeBtn');
  await page.waitForSelector('#probeResult .probe-table');
  const floatRow = await page.locator('#probeResult .probe-table tbody tr', { hasText: /^\s*float/ }).first()
    .locator('td').allInnerTexts();
  check('probe: the CDAB column reads 230.5', floatRow[2] === '230.5', floatRow.join(' | '));
  await closeToasts();
  await page.click('#probeResult .probe-apply button:has-text("CDAB")');
  await page.waitForSelector('.toast');
  const toast = await page.locator('.toast').last().innerText();
  check('probe → CDAB saved and the device restarted', new RegExp(D.tcp).test(toast), toast.replace(/\s+/g, ' '));
  check('template now CDAB', (await tpl(T.tcp)).protocol?.byte_order === 'little');
  v = await waitValues(D.tcp, x => near(x.voltage_l1_n, 230.5), 15);
  check('TCP float CDAB 230.5 V — no container restart', near(v.voltage_l1_n, 230.5), String(v.voltage_l1_n));
  check('TCP int32 CDAB 123456789', v.energy_counter === 123456789, String(v.energy_counter));

  // and the editor path: a no-op protocol save restarts nothing
  const save = await post('/api/device-templates', { device_template: await tpl(T.tcp) });
  check('saving an unchanged protocol restarts nothing', save.status === 200 && (save.body.restarted || []).length === 0,
    JSON.stringify(save.body.restarted));

  // dashboard shows it
  await page.click('[data-page="dashboard"]');
  await page.evaluate(id => window.app.openFleetDevice(id), D.tcp);
  await page.waitForSelector('#dashboardGrid .widget-card[data-address]');
  await page.waitForFunction(() => /230[.,]5/.test(document.getElementById('dashboardGrid')?.innerText || ''), null, { timeout: 10000 }).catch(() => {});
  const grid = await page.innerText('#dashboardGrid');
  check('dashboard shows 230.5 V for the TCP device', /230[.,]5/.test(grid), grid.replace(/\s+/g, ' ').slice(0, 200));
  check('dashboard shows 1234.5 W', /1[ ,.]?234[.,]5/.test(grid), '');

  // ═══ 5. Virtual meter (emulator) ═════════════════════════════════════════
  await page.click('[data-page="vmeters"]');
  await page.waitForSelector('#vmAddInstanceBtn', { state: 'attached' });
  if (await page.isDisabled('#vmAddInstanceBtn')
      && /Add instance/.test(await page.innerText('[data-vmpanel="meters"]')))
    note('Virtual Meters, first visit: "Use “Add instance”" while that button is disabled (no template yet) — nothing says to make a template first, on the Templates sub-tab');
  await page.click('#vmSubtabs [data-vmtab="templates"]');
  await page.click('#vmNewTplBtn');
  await page.waitForSelector('#vmTemplateModal.active');
  await page.fill('#vmfId', VM);
  await page.fill('#vmfName', 'E2E re-served meter');
  await page.selectOption('#vmfByteOrder', 'big');
  const vrow = n => page.locator('#vmRegRows tr').nth(n);
  await vrow(0).locator('.vm-addr').fill('0x0000');
  await vrow(0).locator('.vm-type').selectOption('float');
  await vrow(0).locator('.vm-kind').selectOption('live');
  const srcOpts = await vrow(0).locator('.vm-src-live option').evaluateAll(os => os.map(o => o.value));
  check('vmeter editor lists the TCP device\'s live values', srcOpts.includes(`${D.tcp}.voltage_l1_n`),
    srcOpts.filter(x => x.startsWith('e2e')).slice(0, 4).join(', '));
  await vrow(0).locator('.vm-src-live').selectOption(`${D.tcp}.voltage_l1_n`);
  await page.click('#vmAddRowBtn');
  await vrow(1).locator('.vm-addr').fill('0x0002');
  await vrow(1).locator('.vm-type').selectOption('int32');
  await vrow(1).locator('.vm-kind').selectOption('live');
  await vrow(1).locator('.vm-src-live').selectOption(`${D.tcp}.power_active_total`);
  await vrow(1).locator('.vm-scale').fill('10');
  await page.click('#vmTplSaveBtn');
  await page.waitForSelector('#vmTemplateModal', { state: 'hidden' });
  const vmt = (await api(`/api/virtual-meters/template/${VM}`)).body;
  check('vmeter template saved (2 rows)', (vmt.registers || []).length === 2, JSON.stringify(vmt).slice(0, 160));

  await page.click('[data-page="vmeters"]');
  await page.waitForSelector('#vmAddInstanceBtn:not([disabled])');
  await page.click('#vmAddInstanceBtn');
  await page.waitForSelector('#vmAddInstanceModal.active');
  await page.selectOption('#vmAddDevice', D.tcp);
  await page.selectOption('#vmAddTemplate', VM);
  const vmPort = Number(await page.inputValue('#vmAddPort'));
  await page.locator('#vmAddInstanceModal [data-action="submitAddInstance"]').click();
  await page.waitForSelector('#vmAddInstanceModal', { state: 'hidden' });
  await page.waitForSelector(`.vm-acc[data-mid="${VM}"] input[data-vm]`, { state: 'attached' });
  await page.locator(`.vm-acc[data-mid="${VM}"] label:has(input[data-vm])`).first().click()
    .catch(() => page.locator(`.vm-acc[data-mid="${VM}"] input[data-vm]`).check({ force: true }));
  let inst;
  for (let i = 0; i < 20; i++) {
    inst = ((await api('/api/virtual-meters')).body.instances || []).find(x => x.template === VM);
    if (inst?.state === 'ok') break;
    await sleep(500);
  }
  check('vmeter instance enabled and listening', inst?.state === 'ok', `${inst?.state} port ${inst?.port}`);
  const readPy = 'from pymodbus.client import ModbusTcpClient\nimport struct\n'
    + `c=ModbusTcpClient("127.0.0.1",port=${vmPort});c.connect()\n`
    + 'w=c.read_holding_registers(0,count=4,device_id=1).registers\n'
    + 'print(struct.unpack(">f",struct.pack(">HH",w[0],w[1]))[0], struct.unpack(">i",struct.pack(">HH",w[2],w[3]))[0])\n';
  let out = '';
  try { out = inCtr(`python3 -c '${readPy}'`).trim(); } catch (e) { out = String(e.stderr || e).slice(0, 200); }
  const [vf, vi] = out.split(/\s+/).map(Number);
  check('pymodbus client reads 230.5 from the vmeter (float ABCD)', near(vf, 230.5), out);
  check('…and 12345 (1234.5 W × scale 10, int32)', vi === 12345, out);

  // export → import (as a copy, then the same id is refused without overwrite)
  const exp = (await api(`/api/virtual-meters/template/${VM}/export`)).body;
  check('vmeter template exports as YAML', typeof exp.yaml === 'string' && exp.yaml.includes(`id: ${VM}`), exp.filename);
  const again = await post('/api/virtual-meters/templates/import', { yaml: exp.yaml });
  check('re-import of the same id refused without overwrite (409)', again.status === 409, JSON.stringify(again.body));
  // the UI import (Virtual Meters → Import) with the copy as a file
  await page.click('[data-page="vmeters"]');
  await page.waitForSelector('#vmImportFile', { state: 'attached' });
  await page.setInputFiles('#vmImportFile', { name: `${VM_COPY}.yaml`, mimeType: 'text/yaml',
    buffer: Buffer.from(exp.yaml.replace(`id: ${VM}`, `id: ${VM_COPY}`)) });
  let copy = {};
  for (let i = 0; i < 10 && !(copy.registers || []).length; i++) {
    await sleep(400);
    copy = (await api(`/api/virtual-meters/template/${VM_COPY}`)).body;
  }
  check('UI import of the exported YAML (as a copy) round-trips', JSON.stringify(copy.registers) === JSON.stringify(vmt.registers),
    JSON.stringify(copy).slice(0, 160));

  // ═══ 6. Rules import / export smoke ══════════════════════════════════════
  const rexp = await api('/api/rules/export');
  check('rules export is YAML with a rules: list', rexp.status === 200 && /^rules:/m.test(String(rexp.body)),
    String(rexp.body).slice(0, 80));
  const ruleYaml = [
    'rules:',
    '  - id: e2e-nu-orphan',
    '    label: Points at nothing',
    '    kind: steps',
    '    mode: armed',
    '    signal: e2e-nu-mqtt.power_active_total',
    '    steps: [{at: 1000, value: 50}]',
    '    release_below: 900',
    '    normal: {value: 100}',
    '    target: {device: no-such-device, command: power_limit, param: value}',
  ].join('\n');
  const rimp = await post('/api/rules/import', { yaml: ruleYaml, apply: false });
  const rr = (rimp.body.rules || [])[0] || {};
  check('rules import preview: an orphan target is invalid, with a reason',
    rimp.status === 200 && rr.status === 'invalid' && /no such device/.test((rr.errors || []).join(' ')),
    JSON.stringify(rr));
  check('…the file had it armed and the preview says so', rr.was_armed === true);
  check('preview wrote nothing', !((await api('/api/rules')).body.rules || []).some(x => x.id === 'e2e-nu-orphan'));

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await cleanup();
  stopSims();
}
if (notes.length) console.log(`\nNotes (UX, not failures):\n- ${notes.join('\n- ')}`);
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
