// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// A bridge's bus says how busy its wire was, in words and as a meter: the
// number the gateway measured, never above 100 %, and "over capacity" said
// in words — not "1031 % busy" (D21). The bridges answer is served by a
// route so the values are known; the server side is pinned in
// tests/test_bus_occupancy.py.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 node tools/e2e/bus_occupancy_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};

const dev = (id, unit) => ({ id, name: `Meter ${unit}`, unit_id: unit, endpoint_id: '', health: 'ok', tap: false, bus_share: 0.0925 });
const port = (p, busy) => ({ port: p, label: `bus ${p}`, devices: [1, 2, 3, 4].map(u => dev(`m${p}-${u}`, u)), busy, tapped: false, wire: null });
const bridges = {
  bridges: [{
    id: 'ws', name: 'Panel', type: 'rtu_transparent', type_name: 'RS-485 converter', host: '192.0.2.62', devices: 8,
    state: { status: 'online', detail: '' },
    ports: [
      port(4196, { pct: 37, level: 'ok', over_capacity: false, busy_s: 22.2, window_s: 60, missed_turns: 0, devices: 4, capacity_left_pct: 63 }),
      port(4197, { pct: 100, level: 'full', over_capacity: true, busy_s: 60, window_s: 60, missed_turns: 3, devices: 4, capacity_left_pct: 0 }),
    ],
  }],
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
try {
  for (const lang of ['en', 'ro']) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    page.setDefaultTimeout(15000);
    const errs = [];
    page.on('pageerror', e => errs.push(String(e)));
    await page.route('**/api/bridges', r => r.fulfill({ json: bridges }));
    await page.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); } catch (e) { /* */ } }, lang);
    await page.goto(BASE + '/#/devices');
    await page.waitForLoadState('load');
    const later = page.locator('button:has-text("Later")');
    if (await later.count()) await later.first().click().catch(() => {});
    await page.evaluate(() => window.app.navigateTo('devices'));
    const meters = page.locator('.bridge-card[data-bridge="ws"] .bus-meter');
    await meters.first().waitFor();
    const [a, b] = [meters.nth(0), meters.nth(1)];
    const ta = (await a.innerText()).replace(/\s+/g, ' ').trim();
    const tb = (await b.innerText()).replace(/\s+/g, ' ').trim();
    if (lang === 'en') {
      check('en: the measured value in words', ta === 'busy 37 % · capacity left ~63 %', ta);
      check('en: over capacity is said, never a percentage above 100', tb === 'over capacity' && !/\d{3,}\s*%/.test(tb), tb);
    } else {
      check('ro: the measured value in words', ta === 'ocupată 37 % · capacitate liberă ~63 %', ta);
      check('ro: over capacity in words', tb === 'peste capacitate', tb);
    }
    check(`${lang}: a meter with its value`, (await a.getAttribute('role')) === 'meter'
          && (await a.getAttribute('aria-valuenow')) === '37' && (await a.getAttribute('aria-valuemax')) === '100', '');
    check(`${lang}: the meter says the same words`, (await a.getAttribute('aria-valuetext')) === ta, await a.getAttribute('aria-valuetext'));
    check(`${lang}: the full bus is capped at 100`, (await b.getAttribute('aria-valuenow')) === '100');
    const fill = await a.locator('.bus-meter-fill').evaluate(el => el.style.width);
    check(`${lang}: the bar is drawn at the measured share`, fill === '37%', fill);
    check(`${lang}: the over-capacity state is not colour alone`, (await b.getAttribute('class')).includes('bad') && /capacity|capacitate/.test(tb));
    const title = await a.getAttribute('title');
    check(`${lang}: the tooltip lists each device's share`, /Meter 1: 9%/.test(title), title);
    await a.focus();
    check(`${lang}: the meter can be reached by keyboard`, await page.evaluate(() => document.activeElement?.classList.contains('bus-meter')));
    if (OUT) await page.screenshot({ path: `${OUT}/bus-occupancy-${lang}.png`, fullPage: false });
    check(`${lang}: no page errors`, errs.length === 0, errs.join(' | '));
    await page.close();
  }
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
