// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Line health of the buses behind our serial bridge (ADR-0017), as a user
// meets it. The gateway's answers about the bridge are FAKED in the browser
// (page.route): no bridge and no hardware is needed. One bus on an FTDI with
// frame errors and timeouts (the diagnosis says wiring), one on a CH340 that
// counts no line errors (shown as "not counted", never 0).
//
//   - the bridge card: a health chip per bus, named for a screen reader;
//   - the chip opens the Health dialog: the diagnosis and what to check, the
//     line counters with their rate, the client and the bus's silence, what
//     the gateway saw on that bus, the bridge's events;
//   - the Buses dialog: a Line health column;
//   - Config → Alerts: the bus-health signal (off) and its three limits, and
//     Save sends them;
//   - Romanian words, and a phone-width page that does not scroll sideways.
//
// Against the safety fixture (tools/e2e/fixture_up.sh safety 18087); it saves
// nothing on the gateway (the alerts save is answered by the browser).
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 node tools/e2e/bridge_diag_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + String(extra).slice(0, 300) : ''}`);
};

const now = Date.now() / 1000;
const WIRING = { code: 'wiring', severity: 'warn', confidence: 'high', findings: ['wiring'], evidence: {} };
const OKDX = { code: 'ok', severity: 'ok', confidence: 'low', findings: ['ok'], evidence: {} };
const FTDI_STATS = { line: { frame: 41, parity: 3, overrun: 0, brk: 0, buf_overrun: 0, rx: 9000, tx: 4000 },
  line_note: '', line_verified: true, client: { address: '172.20.0.5:40000', since: now - 3600 },
  connects: 1, connects_10min: 0, silent_s: 2.0, last_rx_ts: now - 2, last_tx_ts: now - 1,
  bytes: { from_device: 9000, to_device: 4000 } };
const CH_STATS = { line: null, line_note: 'the CH340/CH341 driver keeps no line counters', line_verified: true,
  client: { address: '172.20.0.5:40100', since: now - 60 }, connects: 1, connects_10min: 1, silent_s: 1.0,
  last_rx_ts: now - 1, last_tx_ts: now - 1, bytes: { from_device: 100, to_device: 80 } };
const RATES = { window_s: 300, line_errors_per_min: 8.8, frame_per_min: 8.2, parity_per_min: 0.6,
  overrun_per_min: 0, brk_per_min: 0, buf_overrun_per_min: 0 };
const EVENTS = [
  { seq: 7, ts: now - 600, kind: 'client_connected', key: 'B0045K08', tcp_port: 7001, message: 'client 172.20.0.5:40000 connected' },
  { seq: 9, ts: now - 30, kind: 'line_errors', key: 'B0045K08', tcp_port: 7001, message: 'line errors: frame +12, parity +1' },
];
const PORT_FTDI = { adapter: 'B0045K08', label: 'FT232R (B0045K08)', driver: 'ftdi_sio', stats: FTDI_STATS,
  sampled_ts: now - 5, mbg: { ok: 512, timeouts: 14, connection: 0, exceptions: 1, other: 0, crc: 0 },
  rates: RATES, diagnosis: WIRING, events: EVENTS, error: '', supported: true };
const PORT_CH = { adapter: '1a86:7523@1-1', label: 'USB Serial (USB 1-1)', driver: 'ch341-uart', stats: CH_STATS,
  sampled_ts: now - 5, mbg: { ok: 300, timeouts: 0, connection: 0, exceptions: 0, other: 0, crc: 0 },
  rates: { window_s: 300, line_errors_per_min: null }, diagnosis: OKDX, events: [], error: '', supported: true };
const brief = v => ({ diagnosis: v.diagnosis, rates: v.rates, client: v.stats.client.address,
  silent_s: v.stats.silent_s, connects_10min: v.stats.connects_10min, line: v.stats.line,
  line_note: v.stats.line_note, sampled_ts: v.sampled_ts, supported: true, error: '' });
const BRIDGES = { bridges: [{ id: 'e2e-pi', name: 'Pi garage', type: 'mbg_serial_bridge', host: '192.0.2.77',
  has_token: true, type_name: 'Our serial bridge', protocol: 'rtu-tcp', devices: 0,
  state: { status: 'online', detail: '', source: 'bridge' },
  ports: [
    { port: 7001, adapter: 'B0045K08', devices: [], guard: null, busy: null, tapped: false, wire: null, diag: brief(PORT_FTDI) },
    { port: 7002, adapter: '1a86:7523@1-1', devices: [], guard: null, busy: null, tapped: false, wire: null, diag: brief(PORT_CH) },
  ] }] };
const params = { baud: 9600, parity: 'N', databits: 8, stopbits: 1 };
const SCAN = { source: 'bridge', hostname: 'pi5-bench', version: '3.93.0', warnings: [], ports: [
  { port: 7001, key: 'B0045K08', label: 'FT232R (B0045K08)', dev: '/dev/ttyUSB0', kind: 'usb', status: 'ok',
    serial_params: params, serial_text: '9600 8N1', rs485: false, available: true, stats: FTDI_STATS, diag: brief(PORT_FTDI) },
  { port: 7002, key: '1a86:7523@1-1', label: 'USB Serial (USB 1-1)', dev: '/dev/ttyUSB1', kind: 'usb', status: 'ok',
    serial_params: params, serial_text: '9600 8N1', rs485: false, available: true, stats: CH_STATS, diag: brief(PORT_CH) },
] };

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.posted = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  await page.route('**/api/bridges', r => (r.request().method() === 'GET' ? r.fulfill({ json: BRIDGES }) : r.continue()));
  await page.route('**/api/bridges/e2e-pi/diagnostics', r => r.fulfill({ json: {
    fetched_ts: now - 5, error: '', supported: true, version: '3.93.0', events: EVENTS,
    ports: { 7001: PORT_FTDI, 7002: PORT_CH } } }));
  await page.route('**/api/bridges/e2e-pi/scan', r => r.fulfill({ json: SCAN }));
  await page.route('**/api/config/alerts', r => {
    if (r.request().method() === 'POST') {
      page.posted.push(JSON.parse(r.request().postData() || '{}'));
      return r.fulfill({ json: { status: 'ok', channels: ['mqtt'] } });
    }
    return r.fulfill({ json: { enabled: true, mqtt: true, webhook_url: '', webhook_headers: {}, webhook_body: null,
      min_interval_s: 300, latency_ms: 1000, buffer_points: 1000,
      signals: { device: true, sink: true, latency: true, buffer: true, threshold: false, alarm: true, bridge: false },
      bridge: { line_errors_per_min: 10, silent_s: 120, reconnects_per_10min: 6 },
      threshold_deadband_pct: 2, threshold_alert_on_start: true } });
  });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}

async function devices(page) {
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForSelector('.bridge-card[data-bridge="e2e-pi"] .br-diag-chip');
}

try {
  const page = await open();
  await devices(page);
  const chips = page.locator('.bridge-card[data-bridge="e2e-pi"] .br-diag-chip');
  check('card: one health chip per bus', (await chips.count()) === 2);
  check('card: the wiring bus says so, as a warning', /RS-485 wiring/.test(await chips.nth(0).innerText())
        && (await chips.nth(0).getAttribute('class')).includes('warn'), await chips.nth(0).innerText());
  check('card: the chip is a button named for a screen reader',
        (await chips.nth(0).evaluate(e => e.tagName)) === 'BUTTON'
        && /Line health of bus :7001/.test(await chips.nth(0).getAttribute('aria-label')));
  check('card: the CH340 bus says line errors are not counted', /not counted/.test(await chips.nth(1).getAttribute('title')),
        await chips.nth(1).getAttribute('title'));
  await chips.nth(0).click();
  await page.waitForSelector('#bridgeInfoModal.active .br-health');
  const body = page.locator('#bridgeInfoBody');
  const txt = await body.innerText();
  check('health: the diagnosis and what to check', /RS-485 wiring/i.test(txt) && /120 Ω termination/.test(txt), txt);
  const cells = await body.locator('.br-diag-table tbody tr').allInnerTexts();
  check('health: frame errors with their total and rate', /Frame errors\s+41\s+8\.2/.test(cells[0]), cells[0]);
  check('health: a real zero is shown as 0', /UART overruns\s+0\s+0/.test(cells[2]), cells[2]);
  check('health: client, silence and the gateway\'s own counts', /172\.20\.0\.5:40000/.test(txt)
        && /Timeouts\s+14/.test(txt) && /Answered reads\s+512/.test(txt), txt);
  check('health: the bridge\'s events, newest first', (await body.locator('.br-diag-events li').count()) === 2
        && /line errors/.test(await body.locator('.br-diag-events li').first().innerText()));
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));
  await chips.nth(1).click();
  await page.waitForSelector('#bridgeInfoModal.active .br-health');
  const chTxt = await page.locator('#bridgeInfoBody').innerText();
  const chCells = await page.locator('#bridgeInfoBody .br-diag-table tbody tr').allInnerTexts();
  check('health (CH340): every counter "not counted", none shown as 0',
        chCells.length === 5 && chCells.every(c => /not counted/.test(c)), chCells.join(' | '));
  check('health (CH340): the reason, and a low-confidence note', /CH340\/CH341 driver keeps no line counters/.test(chTxt)
        && /cannot be ruled out/.test(chTxt), chTxt);
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));

  // ── the Buses dialog ──
  await page.evaluate(() => window.app.bridgeScan('e2e-pi'));
  await page.waitForSelector('#bridgeInfoModal.active .tplu-table');
  const head = await page.locator('#bridgeInfoBody thead').innerText();
  check('buses: a Line health column, with a chip per row', /Line health/.test(head)
        && (await page.locator('#bridgeInfoBody tbody .br-diag-chip').count()) === 2, head);
  await page.evaluate(() => window.app.closeModal('bridgeInfoModal'));

  // ── Config → Alerts ──
  await page.evaluate(() => { window.app.navigateTo('config'); window.app.switchConfigTab?.('alerts'); window.app.loadAlerts(); });
  await page.waitForFunction(() => document.getElementById('cfgAlertBridgeSilent')?.value === '120');
  check('alerts: the bus-health signal is off, its limits loaded',
        !(await page.locator('#cfgAlertSigBridge').isChecked())
        && (await page.locator('#cfgAlertBridgeLine').inputValue()) === '10'
        && (await page.locator('#cfgAlertBridgeReconnects').inputValue()) === '6');
  check('alerts: every new field has a label', await page.evaluate(() => ['cfgAlertBridgeLine', 'cfgAlertBridgeSilent', 'cfgAlertBridgeReconnects']
        .every(id => document.querySelector(`label[for="${id}"]`))));
  await page.evaluate(() => {
    document.getElementById('cfgAlertSigBridge').checked = true;
    document.getElementById('cfgAlertBridgeSilent').value = '90';
  });
  await page.evaluate(() => window.app.saveAlerts(document.createElement('button')));
  await page.waitForTimeout(400);
  const sent = page.posted.at(-1) || {};
  check('alerts: Save sends the signal and the limits', sent.signals?.bridge === true && sent.bridge?.silent_s === 90
        && sent.bridge?.line_errors_per_min === 10, JSON.stringify(sent));
  check('no page errors (desktop)', page.errs.length === 0, page.errs.join(' | '));

  // ── Romanian ──
  const ro = await open();
  await ro.evaluate(() => window.app.setLanguage('ro', false));
  await ro.waitForTimeout(300);
  await devices(ro);
  const roChip = ro.locator('.br-diag-chip').first();
  check('ro: the chip in Romanian', /cablaj RS-485/.test(await roChip.innerText()), await roChip.innerText());
  await roChip.click();
  await ro.waitForSelector('#bridgeInfoModal.active .br-health');
  const roTxt = await ro.locator('#bridgeInfoBody').innerText();
  check('ro: the Health dialog in Romanian', /Erori de cadru/i.test(roTxt) && /Ce a văzut gateway-ul/i.test(roTxt)
        && /terminația de 120 Ω/.test(roTxt), roTxt);
  check('no page errors (ro)', ro.errs.length === 0, ro.errs.join(' | '));

  // ── phone width ──
  const phone = await open({ width: 390, height: 844 });
  await devices(phone);
  await phone.locator('.br-diag-chip').first().click();
  await phone.waitForSelector('#bridgeInfoModal.active .br-health');
  const over = await phone.evaluate(() => {
    const doc = document.scrollingElement.scrollWidth > window.innerWidth + 1;
    const box = document.querySelector('#bridgeInfoBody .br-health').getBoundingClientRect();
    return { doc, inside: box.right <= window.innerWidth + 1 };
  });
  check('phone: no sideways scroll, the Health dialog stays on the screen', !over.doc && over.inside, JSON.stringify(over));
  check('no page errors (phone)', phone.errs.length === 0, phone.errs.join(' | '));
} catch (e) {
  check('ran to the end', false, e.stack || String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
