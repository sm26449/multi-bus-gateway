/* End-to-end checks for the dashboard fix batch (3.85.0): a slow answer for
 * a device the operator left never lands on the new one (registers and
 * values), the value history reads the device on screen, cards open their
 * history from the keyboard, a silent device gets its own banner, and a
 * remembered device that no longer exists is not restored.
 *
 * Needs an EPHEMERAL instance with auth off and at least two non-primary
 * devices with dashboard registers from DIFFERENT templates (DEV_A / DEV_B). It creates and deletes a
 * TCP device pointed at a closed port for the banner check.
 *
 *   cd tools/e2e && MBG_URL=http://127.0.0.1:8099 DEV_A=seplos-p1 DEV_B=seplos-bank \
 *     node dashboard_fixes_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const A = process.env.DEV_A || 'seplos-p1';
const B = process.env.DEV_B || 'seplos-bank';   // another template: other names
const DOWN = 'down-e2e';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  return { status: r.status, body: await r.json().catch(() => ({})) };
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const names = regs => (regs || []).map(r => r.name).sort().join(',');

await api(`/api/devices/${DOWN}`, { method: 'DELETE' });
const regsA = (await api(`/api/registers/selected?device=${A}`)).body.registers;
const regsB = (await api(`/api/registers/selected?device=${B}`)).body.registers;
check('two devices with dashboard registers, different names', regsA?.length && regsB?.length && names(regsA) !== names(regsB),
  `${A}:${regsA?.length} ${B}:${regsB?.length}`);

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const bad = [];
page.on('response', r => { if (r.status() >= 400) bad.push(`${r.status()} ${r.url().replace(BASE, '')}`); });

try {
  // ---- 1. a remembered device that no longer exists is not restored -------
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('e2e-seeded')) {
      localStorage.setItem('mbg-dash-device', 'ghost-device');
      sessionStorage.setItem('e2e-seeded', '1');
    }
  });
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  const st = await page.evaluate(() => ({ d: window.app.dashDevice, ls: localStorage.getItem('mbg-dash-device') }));
  check('ghost device not restored', st.d === null && st.ls === null, JSON.stringify(st));
  check('dashboard fell back to the primary',
    await page.evaluate(() => window.app._dashIsPrimary()));
  check('primary id comes from the server', await page.evaluate(() => !!window.app._primaryDeviceId()));

  // ---- 2. registers: last device wins ---------------------------------------
  await page.route(`**/api/registers/selected?device=${B}`, async route => {
    await sleep(1500);
    await route.continue();
  });
  await page.evaluate(([a, b]) => { window.app._setDashDevice(b); window.app._setDashDevice(a); }, [A, B]);
  await sleep(2500);
  const shown = await page.evaluate(() => (window.app.dashRegisters || []).map(r => r.name).sort().join(','));
  check('slow answer for the device left behind is dropped', shown === names(regsA),
    `shown=${shown.slice(0, 60)}…`);
  await page.unroute(`**/api/registers/selected?device=${B}`);

  // ---- 3. a value pushed while the snapshot was pending survives -----------
  await page.route(`**/api/values?device=${B}`, async route => {
    await sleep(1200);
    await route.continue();
  });
  const addr = String(regsB[0].address);
  await page.evaluate(([b, addr]) => {
    window._sw = window.app._setDashDevice(b);
    setTimeout(() => window.app.handleWebSocketMessage(
      { type: 'data', device: b, values: { [addr]: { value: 424242, name: 'ws-newer' } } }), 300);
  }, [B, addr]);
  await page.evaluate(() => window._sw);
  const v = await page.evaluate(a => window.app.dashValues[a], addr);
  check('newer socket value not overwritten by the snapshot', v && v.value === 424242, JSON.stringify(v));
  await page.unroute(`**/api/values?device=${B}`);

  // ---- 4. history reads the device on screen; keyboard opens it ------------
  await page.click('[data-page="dashboard"]');
  await page.evaluate(a => window.app.openFleetDevice(a), A);
  await page.waitForSelector('#dashboardGrid .widget-card[data-address]');
  const reqP = page.waitForRequest(r => r.url().includes('/api/history?'));
  const card = page.locator('#dashboardGrid .widget-card[data-address]').first();
  check('card is focusable and announced as a button',
    (await card.getAttribute('tabindex')) === '0' && (await card.getAttribute('role')) === 'button');
  await card.focus();
  await page.keyboard.press('Enter');
  const req = await reqP;
  check('Enter opens the history', await page.isVisible('#valueHistoryModal.active'));
  check('history asks for the device on screen', req.url().includes(`device=${encodeURIComponent(A)}`), req.url());
  await page.evaluate(() => window.app.closeModal('valueHistoryModal'));

  // ---- 5. a silent device gets its own banner ------------------------------
  const mk = await api('/api/devices', { method: 'POST', body: JSON.stringify({
    id: DOWN, enabled: true, template: 'janitza_umg512_pro',
    connection: { protocol: 'tcp', host: '127.0.0.1', port: 9, unit_id: 1, timeout: 1 } }) });
  check('down device created', mk.status === 200, JSON.stringify(mk.body).slice(0, 120));
  let health = '';
  for (let i = 0; i < 25 && !['down', 'stale'].includes(health); i++) {
    await sleep(1000);
    const s = (await api('/api/status')).body;
    health = ((s.devices || []).find(d => d.id === DOWN) || {}).data_health || '';
  }
  check('server reports it not live', ['down', 'stale'].includes(health), health);
  await page.evaluate(async id => { await window.app._fetchDevices(true); await window.app.refreshFleet(); await window.app.openFleetDevice(id); }, DOWN);
  await page.evaluate(async () => {
    const r = await fetch('/api/status'); window.app.status = await r.json(); window.app.updateDashboard();
  });
  check('per-device banner shown', await page.isVisible('#devStaleBadge'),
    await page.locator('#devStaleBadge').textContent().catch(() => ''));
  await page.evaluate(a => window.app.openFleetDevice(a), A);
  await page.evaluate(() => window.app.updateDashboard());
  check('banner gone on a live device', !(await page.isVisible('#devStaleBadge')));

  // the sandbox has no InfluxDB: /api/history answers 503 by design
  // ...and the one 404 that tells the page a remembered device is gone
  const unexpected = bad.filter(b => !/^503 \/api\/history/.test(b)
    && b !== '404 /api/registers/selected?device=ghost-device');
  check('no unexpected HTTP errors', unexpected.length === 0, unexpected.slice(0, 4).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 300));
} finally {
  await api(`/api/devices/${DOWN}`, { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
