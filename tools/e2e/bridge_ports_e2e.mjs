// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Board UARTs in the pickers (ADR-0016), as a user meets them. The serial
// bridge's answers are FAKED in the browser (page.route), so no bridge and no
// hardware is needed: a USB adapter and HAT channel 0 that it serves, HAT
// channel 1 the container may not open yet (no-access, with the fix), and the
// kernel console (reserved).
//
//   - A bridge's Buses dialog: served ports are rows (RS-485 box only on the
//     board port, and Save sends it); the others are listed under the table,
//     each saying why, the no-access one with its command and a Copy button
//     that copies exactly that command; the bridge's warning on top.
//     (Until L3.9 the same list also stood under Add Device's "RTU over the
//     network → Scan" picker; the Connection step reaches a bridge's bus as a
//     bridge record instead — FINDINGS L3.9-F2.)
//   - Romanian words, and a phone-width page that does not scroll sideways.
//
// Against the safety fixture (tools/e2e/fixture_up.sh safety 18087); it saves
// nothing on the gateway.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 node tools/e2e/bridge_ports_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + String(extra).slice(0, 300) : ''}`);
};

const FIX = 'sh scripts/serial-access.sh';
const params = { baud: 9600, parity: 'N', databits: 8, stopbits: 1 };
const ADAPTERS = [
  { stable_id: '1a86:7523@1-1', dev: '/dev/ttyUSB0', tcp_port: 7001, available: true, excluded: false,
    kind: 'usb', label: 'USB Serial (USB 1-1)', status: 'ok', reason: '', fix: '', model: 'USB Serial',
    serial: '', serial_params: params, serial_text: '9600 8N1', rs485: false, bridge_host: 'mbg-serial-bridge' },
  { stable_id: 'spi0.0:0', dev: '/dev/ttySC0', tcp_port: 7003, available: true, excluded: false,
    kind: 'board', label: 'SC16IS752 ch0 (spi0.0)', status: 'ok', reason: '', fix: '', model: '', serial: '',
    serial_params: params, serial_text: '9600 8N1', rs485: false, bridge_host: 'mbg-serial-bridge' },
  { stable_id: 'spi0.0:1', dev: '/dev/ttySC1', tcp_port: null, available: false, excluded: false,
    kind: 'board', label: 'SC16IS752 ch1 (spi0.0)', status: 'no-access', needs_major: 509,
    reason: 'the container may not open device major 509', fix: FIX, model: '', serial: '',
    serial_params: params, serial_text: '9600 8N1', rs485: false, bridge_host: 'mbg-serial-bridge' },
  { stable_id: '107d001000.serial', dev: '/dev/ttyAMA10', tcp_port: null, available: false, excluded: false,
    kind: 'board', label: 'pl011-axi (107d001000.serial)', status: 'reserved',
    reason: 'kernel console (console=ttyAMA10 in /proc/cmdline)', fix: '', model: '', serial: '',
    serial_params: params, serial_text: '9600 8N1', rs485: false, bridge_host: 'mbg-serial-bridge' },
  { stable_id: '00:05', dev: '/dev/ttyS0', tcp_port: null, available: false, excluded: false,
    kind: 'board', label: 'serial (00:05)', status: 'not-enabled', needs_enable: true, enabled: false,
    reason: 'a UART of the board itself: served only once enabled', fix: '', model: '', serial: '',
    serial_params: params, serial_text: '9600 8N1', rs485: false, bridge_host: 'mbg-serial-bridge' },
];
// a bus the gateway's guard keeps off the wire: its adapter moved to :7005
const GUARD = [{ host: '192.0.2.50', port: 7001, status: 'moved', allowed: false, adapter: 'B0045K08',
  now_at: 7005, detail: 'the adapter B0045K08 is now on :7005, and :7001 is OTHER — not read' }];
// the bridge's own warning: a device-rule slot a kernel update left stale
const WARN = [{ slot: 1, rule: '509:*', driver: 'sc16is7xx', fix: FIX,
  message: 'device rule slot BRIDGE_TTY_1=509:* (written for sc16is7xx): no device has that number now' }];
// what GET /api/bridges/<id>/scan makes of the same answer (multibus/routes/bridges.py)
const SCAN = {
  source: 'bridge', hostname: 'pi5-bench', version: '3.93.0',
  ports: ADAPTERS.map(a => ({ port: a.status === 'ok' ? a.tcp_port : null, key: a.stable_id, label: a.label,
    dev: a.dev, kind: a.kind, status: a.status, reason: a.reason, fix: a.fix, serial_params: params,
    serial_text: '9600 8N1', rs485: false, available: a.status === 'ok', excluded: false,
    needs_enable: !!a.needs_enable, enabled: !!a.enabled })),
  warnings: WARN,
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }) {
  const ctx = await browser.newContext({ viewport, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.posted = [];
  page.dlg = await watchDialogs(page);
  await page.route('**/api/bridge-guard', r => r.fulfill({ json: { problems: GUARD } }));
  await page.route('**/api/bridge-guard/follow', r => {
    page.posted.push({ url: r.request().url(), body: JSON.parse(r.request().postData() || '{}') });
    return r.fulfill({ json: { moved: { 7001: 7005 }, devices: ['m1'] } });
  });
  await page.route('**/api/bridge/adapters', r => r.fulfill({ json: { available: true, bridge_host: 'mbg-serial-bridge',
    version: '3.93.0', hostname: 'pi5-bench', auth: false, adapters: ADAPTERS, warnings: WARN } }));
  await page.route('**/api/bridges/e2e-pi/ports/*/enable', r => {
    page.posted.push({ url: r.request().url(), body: JSON.parse(r.request().postData() || '{}') });
    return r.fulfill({ json: { stable_id: '00:05', label: 'serial (00:05)', status: 'ok', enabled: true } });
  });
  await page.route('**/api/bridges/e2e-pi/scan', r => r.fulfill({ json: SCAN }));
  await page.route('**/api/bridges/e2e-pi/ports/*/serial', r => {
    page.posted.push({ url: r.request().url(), body: JSON.parse(r.request().postData() || '{}') });
    return r.fulfill({ json: { stable_id: 'spi0.0:0', serial_text: '9600 8N1 RS-485', rs485: true } });
  });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}

// the bridge's Buses dialog, with what the bridge answered
async function busesDialog(page) {
  await page.evaluate(() => window.app.bridgeScan('e2e-pi'));
  await page.waitForSelector('#bridgeInfoModal.active');
  await page.waitForSelector('#bridgeInfoBody .br-blocked-list');
}

try {
  // ── a bridge's Buses dialog ──
  const page = await open();
  await busesDialog(page);
  const rows = page.locator('#bridgeInfoBody tbody tr');
  check('buses: only the served ports are rows', (await rows.count()) === 2
        && /:7001/.test(await rows.nth(0).innerText()) && /:7003/.test(await rows.nth(1).innerText()));
  const ne = page.locator('#bridgeInfoBody .br-blocked-item[data-status="not-enabled"]');
  check('not-enabled: says why, and offers to enable it here',
        /cannot be seen/.test(await ne.innerText()) && (await ne.locator('button:has-text("Enable")').count()) === 1, await ne.innerText());
  const warn = page.locator('#bridgeInfoBody [data-warning]');
  check('stale device rule: shown with its command', (await warn.count()) === 1
        && /out of date/.test(await warn.innerText()) && /sc16is7xx/.test(await warn.innerText())
        && (await warn.locator('.br-fix-cmd').innerText()) === FIX, await warn.innerText().catch(() => ''));
  const na = page.locator('#bridgeInfoBody .br-blocked-item[data-status="no-access"]');
  check('no-access: says what to do, the command and the bridge\'s reason',
        /may not open this port yet/.test(await na.innerText())
        && (await na.locator('.br-fix-cmd').innerText()) === FIX
        && /major 509/.test(await na.innerText()), await na.innerText());
  const res = page.locator('#bridgeInfoBody .br-blocked-item[data-status="reserved"]');
  check('reserved: says the system uses it, offers no command',
        /kernel console or Bluetooth/.test(await res.innerText()) && (await res.locator('.br-fix').count()) === 0,
        await res.innerText());
  const copy = na.locator('.br-fix button');
  check('copy button: named for a screen reader', (await copy.getAttribute('aria-label')) === 'Copy the command');
  await copy.click();
  await page.waitForTimeout(200);
  check('copy button: copies exactly the command', (await page.evaluate(() => navigator.clipboard.readText())) === FIX);
  check('buses: each row says its kind', /USB/i.test(await rows.nth(0).innerText()) && /Board UART/i.test(await rows.nth(1).innerText()));
  check('buses: RS-485 box on the board port only', (await rows.nth(0).locator('.br-rs485').count()) === 0
        && (await rows.nth(1).locator('.br-rs485').count()) === 1);
  check('buses: the others are listed under the table', (await page.locator('#bridgeInfoBody .br-blocked-list .br-blocked-item').count()) === 3);
  check('buses: the bridge\'s warning on top', (await page.locator('#bridgeInfoBody [data-warning]').count()) === 1);
  await rows.nth(1).locator('.br-rs485').check();
  await rows.nth(1).locator('button:has-text("Save")').click();
  await page.waitForTimeout(300);
  const p = page.posted.at(-1) || {};
  check('buses: Save sends RS-485 for that port only', /ports\/spi0\.0%3A0\/serial$/.test(p.url || '')
        && p.body && p.body.rs485 === true && p.body.baud === 9600, JSON.stringify(p));
  // enable a SoC UART: a warning first, then the opt-in is sent
  await page.locator('#bridgeInfoBody .br-blocked-item[data-status="not-enabled"] button:has-text("Enable")').click();
  await page.waitForTimeout(400);
  const asked = page.dlg.seen.at(-1) || {};
  const pe = page.posted.at(-1) || {};
  check('buses: Enable warns before serving a SoC UART', /nothing else uses it/.test(asked.message || ''), JSON.stringify(asked));
  check('buses: Enable sends the opt-in for that port', /ports\/00%3A05\/enable$/.test(pe.url || '')
        && pe.body && pe.body.enabled === true, JSON.stringify(pe));
  // ── the Devices page: a moved bus is said, and followed in one click ──
  await page.evaluate(() => window.app.closeModal?.('bridgeInfoModal'));
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForSelector('#guardBanner', { timeout: 10000 }).catch(() => {});
  const gb = page.locator('#guardBanner');
  check('devices: a moved bus is shown, not read, with where its adapter went',
        (await gb.count()) === 1 && /B0045K08 is now on :7005/.test(await gb.innerText()), await gb.innerText().catch(() => ''));
  await gb.locator('button:has-text("Follow the adapters")').click();
  await page.waitForTimeout(400);
  const pf = page.posted.at(-1) || {};
  check('devices: Follow asks first, then follows that bridge', /restarts there/.test((page.dlg.seen.at(-1) || {}).message || '')
        && /bridge-guard\/follow$/.test(pf.url || '') && pf.body.host === '192.0.2.50', JSON.stringify(pf));
  check('no page errors (desktop)', page.errs.length === 0, page.errs.join(' | '));

  // ── Romanian ──
  const ro = await open();
  await ro.evaluate(() => window.app.setLanguage('ro', false));
  await ro.waitForTimeout(300);
  await busesDialog(ro);
  const roText = await ro.locator('#bridgeInfoBody .br-blocked').first().innerText();
  check('ro: statuses and hints in Romanian', /fără acces/i.test(roText) && /rezervat/i.test(roText)
        && /Containerul bridge-ului/.test(roText) && /Porturi care nu pot fi folosite încă/i.test(roText), roText);
  check('ro: kind in Romanian', /UART de placă/i.test(await ro.locator('#bridgeInfoBody tbody').innerText()));
  check('no page errors (ro)', ro.errs.length === 0, ro.errs.join(' | '));

  // ── phone width ──
  const phone = await open({ width: 390, height: 844 });
  await busesDialog(phone);
  const over = await phone.evaluate(() => {
    const doc = document.scrollingElement.scrollWidth > window.innerWidth + 1;
    const list = document.querySelector('.br-blocked:not([hidden])');
    const box = list?.getBoundingClientRect();
    return { doc, inside: !!box && box.right <= window.innerWidth + 1 };
  });
  check('phone: no sideways scroll, the list stays on the screen', !over.doc && over.inside, JSON.stringify(over));
  check('no page errors (phone)', phone.errs.length === 0, phone.errs.join(' | '));
} catch (e) {
  check('ran to the end', false, e.stack || String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
