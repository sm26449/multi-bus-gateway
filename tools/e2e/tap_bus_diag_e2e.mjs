// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A listen-only tapped bus in the Line health dialog (TAP-DIAG-2): eight
// Seplos packs share one reader behind our bridge. The bus's own counters
// (CRC, orphans, resync, unpaired, paired-across-junk) are shown ONCE for the
// bus, the resync's junk sample ring as hex, and per pack only its own
// (windows, values, implausible drops, last value). The gateway's answers are
// FAKED in the browser (page.route): no bridge and no hardware is needed.
//
// Against the safety fixture (tools/e2e/fixture_up.sh safety 18087); it saves
// nothing on the gateway.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 node tools/e2e/tap_bus_diag_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + String(extra).slice(0, 400) : ''}`);
};

const now = Date.now() / 1000;
const OKDX = { code: 'ok', severity: 'ok', confidence: 'high', findings: ['ok'], evidence: {} };
const STATS = { line: { frame: 0, parity: 0, overrun: 0, brk: 0, buf_overrun: 0, rx: 9000, tx: 0 },
  line_note: '', line_verified: true, client: { address: '172.20.0.5:40000', since: now - 3600 },
  connects: 1, connects_10min: 0, silent_s: 0.5, last_rx_ts: now - 1, last_tx_ts: null,
  bytes: { from_device: 9000, to_device: 0 } };
const N = 8;
const DEVS = Array.from({ length: N }, (_, i) => ({
  id: `seplos-tap-p${i + 1}`, windows: 1000 + i, updates: 40000 + i,
  implausible: i === 2 ? 5 : 0, implausible_windows: i === 2 ? 1 : 0, staleness_age_s: 2, connected: true }));
const TAP_BUS = { port: '192.0.2.77:7003', open_error: '', frames: 2680000, writes_seen: 0,
  crc_errors: 3, orphan_frames: 1, exception_responses: 0, resync_dropped_bytes: 1234,
  inferred_windows: 12, paired_across_junk: 2, unpaired_responses: 4,
  junk_samples: [{ ts: now - 120, bytes: 12, hex: '000000000000000000000000' },
                 { ts: now - 30, bytes: 7, hex: 'ff01fe0203a5c3' }],
  devices: DEVS };
// the window: 8 taps on one bus — the legacy sums say 8×, the deduped once
const MBG = { ok: 800, timeouts: 0, connection: 0, exceptions: 0, other: 0, crc: 24,
  crc_deduped: 3, exceptions_deduped: 0, implausible: 5 };
const PORT = { adapter: 'B0045K09', label: 'FT232R (B0045K09)', driver: 'ftdi_sio', stats: STATS,
  sampled_ts: now - 5, mbg: MBG, tap_bus: TAP_BUS, rates: { window_s: 300, line_errors_per_min: 0 },
  diagnosis: OKDX, events: [], error: '', supported: true };
const brief = v => ({ diagnosis: v.diagnosis, rates: v.rates, client: v.stats.client.address,
  silent_s: v.stats.silent_s, connects_10min: v.stats.connects_10min, line: v.stats.line,
  line_note: v.stats.line_note, sampled_ts: v.sampled_ts, supported: true, error: '' });
const BRIDGES = { bridges: [{ id: 'e2e-tap', name: 'Pi battery room', type: 'mbg_serial_bridge', host: '192.0.2.77',
  has_token: true, type_name: 'Our serial bridge', protocol: 'rtu-tcp', devices: N,
  state: { status: 'online', detail: '', source: 'bridge' },
  ports: [{ port: 7003, adapter: 'B0045K09', guard: null, busy: null, tapped: true, diag: brief(PORT),
    wire: { frames: 2680000, crc_errors: 3, error: '', orphan_frames: 1, exception_responses: 0,
            resync_dropped_bytes: 1234, unpaired_responses: 4, paired_across_junk: 2 },
    devices: DEVS.map((d, i) => ({ id: d.id, name: `Pack ${i + 1}`, unit_id: i + 1, endpoint_id: '',
      health: 'ok', tap: true, bus_share: null })) }] }] };

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  await page.route('**/api/bridges', r => (r.request().method() === 'GET' ? r.fulfill({ json: BRIDGES }) : r.continue()));
  await page.route('**/api/bridges/e2e-tap/diagnostics', r => r.fulfill({ json: {
    fetched_ts: now - 5, error: '', supported: true, version: '3.100.1', events: [], ports: { 7003: PORT } } }));
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}

async function health(page) {
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForSelector('.bridge-card[data-bridge="e2e-tap"] .br-diag-chip');
  await page.locator('.bridge-card[data-bridge="e2e-tap"] .br-diag-chip').first().click();
  await page.waitForSelector('#bridgeInfoModal.active .br-health [data-tap-bus]');
  return page.locator('#bridgeInfoBody');
}

try {
  const page = await open();
  const body = await health(page);
  // the second <dl> is "What the gateway saw on this bus" (the first: activity)
  const mbgTxt = await body.locator('.br-health > dl.br-diag-dl').nth(1).innerText();
  check('the gateway\'s CRC on this bus is the bus\'s, once (3, never 24)',
        /CRC errors\s+3\b/.test(mbgTxt) && !/\b24\b/.test(mbgTxt), mbgTxt);
  const tap = body.locator('[data-tap-bus]');
  const tapTxt = await tap.innerText();
  check('the tapped bus section names its counters once',
        /Valid frames\s+2680000/.test(tapTxt) && /CRC errors\s+3\b/.test(tapTxt)
        && /Frames with no partner\s+1\b/.test(tapTxt) && /Bytes skipped to realign\s+1234/.test(tapTxt), tapTxt);
  check('unpaired responses and paired-across-junk are shown',
        /Responses with no matching request\s+4\b/.test(tapTxt) && /Paired across skipped bytes\s+2\b/.test(tapTxt), tapTxt);
  const junk = await tap.locator('table.br-junk tbody tr').allInnerTexts();
  check('the junk ring: two runs, newest first, hex in byte pairs',
        junk.length === 2 && /\b7\b/.test(junk[0]) && /ff 01 fe 02 03 a5 c3/.test(junk[0]) && /\b12\b/.test(junk[1]),
        junk.join(' | '));
  const devRows = await tap.locator('table.br-tap-devs tbody tr').allInnerTexts();
  check('one row per listening pack, with only its own counters', devRows.length === N
        && /seplos-tap-p3\s+1002\s+40002\s+5/.test(devRows[2]) && /1 windows dropped/.test(devRows[2])
        && devRows.filter((r, i) => i !== 2).every(r => /\s0\s/.test(r)), devRows.join(' | '));
  check('no bus counter in a pack row', !devRows.some(r => /1234|2680000/.test(r)));
  check('no page errors (desktop)', page.errs.length === 0, page.errs.join(' | '));

  // ── Romanian ──
  const ro = await open();
  await ro.evaluate(() => window.app.setLanguage('ro', false));
  await ro.waitForTimeout(300);
  const roBody = await health(ro);
  const roTxt = await roBody.locator('[data-tap-bus]').innerText();
  // headings are upper-cased by the stylesheet: compare without case
  check('ro: the tapped bus in Romanian', /Magistrala ascultată/i.test(roTxt) && /Octeți săriți la realiniere/i.test(roTxt)
        && /Răspunsuri fără cererea lor/i.test(roTxt) && /Primii octeți \(hex\)/i.test(roTxt), roTxt);
  check('no page errors (ro)', ro.errs.length === 0, ro.errs.join(' | '));

  // ── phone width ──
  const phone = await open({ width: 390, height: 844 });
  await health(phone);
  const over = await phone.evaluate(() => {
    const doc = document.scrollingElement.scrollWidth > window.innerWidth + 1;
    const box = document.querySelector('#bridgeInfoBody [data-tap-bus]').getBoundingClientRect();
    return { doc, inside: box.right <= window.innerWidth + 1 };
  });
  check('phone: no sideways scroll, the tap section stays on the screen', !over.doc && over.inside, JSON.stringify(over));
  check('no page errors (phone)', phone.errs.length === 0, phone.errs.join(' | '));
} catch (e) {
  check('ran to the end', false, e.stack || String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
