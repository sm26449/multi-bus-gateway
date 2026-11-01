// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The guide findings UI-G1..G4 in a browser, on the demo gateway
// (tools/demo/demo_up.sh: the UPS measured by its meters, the primary meter
// with its power factor and THD, InfluxDB for History). Read-only.
//   G1  the UPS's fleet row shows what its meters give it — the numbers
//       /api/fleet and its own page's source (/api/values) carry
//   G2  Add Device: the line under the connection choice follows the choice
//   G3  History: the chart draws (the axis placement itself is checked by
//       ui_g_findings_test.mjs; look at the screenshots in SHOTS_DIR)
//   G4  Monitor: every group heading is a name, in the page's language
//
//   cd tools/e2e && MBG_URL=http://127.0.0.1:18100 node ui_g_findings_e2e.mjs
import { chromium } from 'playwright';
import fs from 'node:fs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18100';
const SHOTS = process.env.SHOTS_DIR || './shots';
fs.mkdirSync(SHOTS, { recursive: true });
const results = [];
const check = (name, cond, extra = '') => {
  results.push(!!cond);
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const api = async p => (await fetch(BASE + p)).json();
const sleep = ms => new Promise(r => setTimeout(r, ms));
const en = JSON.parse(fs.readFileSync(new URL('../../ui/languages/en.json', import.meta.url), 'utf8'));
const ro = JSON.parse(fs.readFileSync(new URL('../../ui/languages/ro.json', import.meta.url), 'utf8'));

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const open = async (lang, size = { width: 1440, height: 950 }) => {
  const ctx = await browser.newContext({ viewport: size, locale: 'en-GB' });
  await ctx.addInitScript(l => { try { localStorage.setItem('mbg-lang', l); localStorage.setItem('mbg-monitor-visited', 'true'); } catch (e) {} }, lang);
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  await sleep(2500);
  return { ctx, page, errs };
};
const go = async (page, hash, wait = 3500) => { await page.evaluate(h => { location.hash = h; }, hash); await sleep(wait); };

// ── G1: value source → API → screen ─────────────────────────────────────────
{
  const { ctx, page, errs } = await open('en');
  await go(page, '#/dashboard');
  const row = page.locator('.fleet-row[data-id="ups"]');
  check('G1: the UPS has a fleet row', await row.count() === 1);
  // the screen and the API, read together (values move every few seconds)
  let ok = false, last = null;
  for (let i = 0; i < 5 && !ok; i++) {
    const [fleet, vals] = await Promise.all([api('/api/fleet'), api('/api/values?device=ups')]);
    await page.evaluate(() => window.app && window.app.refreshFleet && window.app.refreshFleet());
    await sleep(300);
    const hero = (fleet.devices.find(d => d.id === 'ups') || {}).hero || [];
    const page_ = Object.fromEntries(Object.values(vals.values || {}).map(v => [v.name, v.value]));
    const labels = await row.locator('.fm-l').allInnerTexts();
    last = { hero, labels };
    ok = hero.length === 3 && hero.every(h => h.meters && h.value != null && page_[h.name] != null)
      && JSON.stringify(labels) === JSON.stringify([en['meters.in'], en['meters.out'], en['meters.eff']]);
    if (!ok) await sleep(1500);
  }
  check('G1: the row names In / Out / Efficiency, the API gives them from its meters', ok, last);
  const shown = (await row.locator('.fm-v').allInnerTexts()).map(s => Number(s.replace(/[^\d.-]/g, '')));
  check('G1: each shows a number', shown.length === 3 && shown.every(Number.isFinite), shown);
  // the same reading on the device page's source: In ≈ the input meter's power
  const meterIn = Object.values((await api('/api/values?device=ups-in')).values || {}).find(v => v.name === 'power_active_total');
  const upsIn = Object.values((await api('/api/values?device=ups')).values || {}).find(v => v.name === 'power_in');
  check('G1: In is the input meter\'s power (W, same reading)', meterIn && upsIn && Math.abs(meterIn.value - upsIn.value) < Math.max(25, meterIn.value * 0.05),
        { meter: meterIn?.value, ups: upsIn?.value });
  await row.screenshot({ path: `${SHOTS}/g1-ups-row.png` });
  check('G1: no page errors', errs.length === 0, errs);
  await ctx.close();
}

// ── G2: the connection choice says what each answer means ──────────────────
// Until L3.9 a line under the old form's protocol choice said it; the
// Connection step (conn-step.js) asks port → where → role → protocol, every
// option shown, the ones that cannot be taken with their reason beside them,
// and the role's own explanation under the question.
for (const [lang, d] of [['en', en], ['ro', ro]]) {
  const { ctx, page, errs } = await open(lang);
  await go(page, '#/devices');
  await page.click('[data-action="openDeviceWizard"]');
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
  await page.click('[data-ad-way="connection"]');
  await page.waitForSelector('#csAddPortKindQ');
  const legend = await page.locator('#csAddPortKindQ legend').innerText();
  check(`G2 ${lang}: the port question is a named radiogroup`, (await page.locator('#csAddPortKindQ [role="radiogroup"]').count()) === 1
        && legend.trim().length > 0, legend);
  await page.locator('label:has(#csAddPortKind-serial)').click();
  await sleep(400);
  check(`G2 ${lang}: focus stays on the choice after a change`, await page.evaluate(() => document.activeElement?.name === 'csAddPortKind'));
  await page.locator('label:has(#csAddReach-here)').click().catch(() => {});
  await sleep(400);
  const roleQ = await page.locator('#csAddRoleQ').innerText().catch(() => '');
  check(`G2 ${lang}: the role question says what each answer means`, roleQ.includes((d['cs.role.master'] || 'Master').split(' ')[0]) && roleQ.length > 40, roleQ);
  if (lang === 'en') await page.screenshot({ path: `${SHOTS}/g2-add-connection.png` });
  check(`G2 ${lang}: no page errors`, errs.length === 0, errs);
  await ctx.close();
}

// ── G3: History draws ───────────────────────────────────────────────────────
for (const [w, h] of [[1440, 950], [390, 844]]) {
  const { ctx, page, errs } = await open('en', { width: w, height: h });
  await go(page, '#/devices/umg512/history', 6000);
  const cv = page.locator('#historyCanvas');
  check(`G3 ${w}px: the chart is there`, await cv.count() === 1 && (await cv.boundingBox())?.width > 200);
  await cv.screenshot({ path: `${SHOTS}/g3-history-${w}.png` });
  check(`G3 ${w}px: no page errors`, errs.length === 0, errs);
  await ctx.close();
}

// ── G4: the Monitor's group headings ────────────────────────────────────────
for (const [lang, d] of [['en', en], ['ro', ro]]) {
  const { ctx, page, errs } = await open(lang);
  await go(page, '#/devices/umg512/monitor');
  const heads = await page.$$eval('#monitorCategories .monitor-category-header > span:nth-child(2)', s => s.map(x => x.textContent.trim()));
  check(`G4 ${lang}: headings are names, never keys`, heads.length > 3 && heads.every(x => !x.includes('_')), heads);
  check(`G4 ${lang}: the power factor group is named`, heads.includes(d['sec.power_factor']), heads);
  const first = page.locator('#monitorCategories .monitor-category-header').first();
  await first.focus();
  await page.keyboard.press('Enter');
  check(`G4 ${lang}: a heading folds from the keyboard`, (await first.getAttribute('aria-expanded')) === 'false');
  await page.keyboard.press(' ');
  check(`G4 ${lang}: and opens again`, (await first.getAttribute('aria-expanded')) === 'true');
  check(`G4 ${lang}: no page errors`, errs.length === 0, errs);
  await ctx.close();
}

await browser.close();
const bad = results.filter(x => !x).length;
console.log(`${results.length - bad}/${results.length} passed`);
process.exit(bad ? 1 : 0);
