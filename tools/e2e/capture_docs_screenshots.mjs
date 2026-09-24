/* Regenerate the documentation screenshots (docs/img/guide/*.png and
 * docs/img/vm-logs.png) from a running gateway, so they never drift from
 * the interface: same viewport, light theme, English, admin logged in.
 *
 * Against a demo instance (never production): a primary Janitza with
 * selected registers, two EM24 units, an installation with an inverter
 * group that offers commands, a virtual meter, a rule, a snapshot.
 *
 *   MBG_URL=http://localhost:18090 MBG_USER=admin MBG_PASS=… \
 *   CHROME_PATH=<chromium> node capture_docs_screenshots.mjs
 *
 * OUT_DIR overrides the repo's docs/img (default: ../../docs/img).
 */
import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';

const BASE = process.env.MBG_URL || 'http://localhost:18090';
const USER = process.env.MBG_USER || 'admin';
const PASS = process.env.MBG_PASS || '';
const EXEC = process.env.CHROME_PATH;
const OUT = process.env.OUT_DIR || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../docs/img');
const GUIDE = { width: 1440, height: 950 };

const br = await chromium.launch(EXEC ? { executablePath: EXEC } : {});
const ctx = await br.newContext({ viewport: GUIDE, deviceScaleFactor: 1, locale: 'en-US' });
await ctx.addInitScript(() => { try { localStorage.setItem('mbg-theme', 'light'); localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
const page = await ctx.newPage();
page.setDefaultTimeout(20000);
const errs = [];
page.on('pageerror', e => errs.push(String(e).slice(0, 160)));
const wait = (ms) => page.waitForTimeout(ms);
const shot = async (name, opts = {}) => {
    await page.evaluate(() => document.querySelectorAll('.toast').forEach(t => t.remove()));
    await page.screenshot({ path: `${OUT}/${name}`, ...opts });
    console.log('  ✓', name);
};
const go = async (pg, ms = 1500) => { await page.click(`[data-page="${pg}"]`); await wait(ms); };
const closeModals = () => page.evaluate(() => document.querySelectorAll('.modal.active').forEach(m => m.classList.remove('active')));
const top = () => page.evaluate(() => window.scrollTo(0, 0));
const openUnit = async (unitId, tab, ms = 2500) => {
    await go('devices', 1200);
    await page.evaluate(() => { try { app.closeDeviceDetail(); } catch (e) {} try { app.closeEndpointDetail(); } catch (e) {} });
    await wait(400);
    await page.locator(`[data-action="openDeviceDetail"][data-args='["${unitId}"]']`).first().click();
    await wait(2000);
    if (tab) { await page.locator(`#deviceWsTabs .config-main-tab[data-dtab="${tab}"]`).click(); await wait(ms); }
};

// ---- login ----------------------------------------------------------------
await page.goto(BASE + '/');
await page.waitForSelector('#loginUser', { state: 'visible' });
await page.fill('#loginUser', USER);
await page.fill('#loginPass', PASS);
await page.click('#loginBtn');
await page.waitForSelector('#loginOverlay', { state: 'hidden', timeout: 20000 });
await wait(1500);
const later = page.locator('button:has-text("Later")');
if (await later.count()) await later.first().click().catch(() => {});
await page.evaluate(() => { try { app.dismissFirstRun(); } catch (e) {} });
await wait(500);

// ---- 01 dashboard ------------------------------------------------------------
await go('dashboard', 2500); await top(); await shot('guide/01-dashboard.png');

// ---- 02 monitor (a unit's Monitor tab with a few live values) ----------------
await openUnit('umg512', 'monitor', 1500);
await page.evaluate(async () => {
    const r = await (await fetch('/api/registers/selected')).json();
    const want = ['voltage_l1_n', 'voltage_l2_n', 'voltage_l3_n'];
    for (const n of want) {
        const x = (r.registers || []).find(z => z.name === n);
        if (x) app.addToMonitor({ address: x.address, name: x.name, description: x.label || x.description || x.name, unit: x.unit || '', dataType: x.data_type || 'float' });
    }
});
await wait(4000); await top(); await shot('guide/02-monitor.png');

// ---- 03 history --------------------------------------------------------------
await openUnit('umg512', 'history', 2500);
await page.evaluate(() => document.querySelectorAll('#historyPage .hint-banner-dismiss, [data-action="dismissMonitorHint"]').forEach(b => b.click()));
await page.selectOption('#histRange', '-1h').catch(() => {});
for (const name of ['voltage_l1_n', 'voltage_l2_n', 'voltage_l3_n']) {
    await page.evaluate((n) => { const it = document.querySelector(`#histRegList .hist-item[data-name="${n}"]`); if (it && !it.classList.contains('selected')) it.click(); }, name);
    await wait(900);
}
await wait(3500); await top(); await shot('guide/03-history.png');

// ---- 04 devices (installations + standalone units) ----------------------------
await go('devices', 1500);
await page.evaluate(() => { try { app.closeDeviceDetail(); } catch (e) {} try { app.closeEndpointDetail(); } catch (e) {} });
await wait(1200); await top(); await shot('guide/04-devices.png');

// ---- 05 templates ---------------------------------------------------------------
await go('templates', 2000); await top(); await shot('guide/05-templates.png');

// ---- 06 status ------------------------------------------------------------------
await go('status', 3000); await top(); await shot('guide/06-status.png');

// ---- 07 virtual meters (card expanded on Overview) --------------------------------
await go('vmeters', 2000);
const head = page.locator('.vm-acc-head').first();
if (await head.count()) { await head.click(); await wait(1500); }
await top(); await shot('guide/07-vmeters.png');

// ---- vm-logs (README) — the Logs sub-tab, 2x ----------------------------------------
const logsTab = page.locator('[data-vmsub="logs"]').first();
if (await logsTab.count()) {
    await logsTab.click(); await wait(2500);
    const p2 = await ctx.newPage(); await p2.close();   // keep session; use a fresh 2x context below
    const ctx2 = await br.newContext({ viewport: { width: 1600, height: 980 }, deviceScaleFactor: 2, storageState: await ctx.storageState() });
    await ctx2.addInitScript(() => { try { localStorage.setItem('mbg-theme', 'light'); } catch (e) {} });
    const pg = await ctx2.newPage();
    await pg.goto(BASE + '/'); await pg.waitForTimeout(2000);
    await pg.evaluate(() => { try { app.dismissFirstRun(); } catch (e) {} });
    await pg.click('[data-page="vmeters"]'); await pg.waitForTimeout(2000);
    const h2 = pg.locator('.vm-acc-head').first(); if (await h2.count()) { await h2.click(); await pg.waitForTimeout(1200); }
    const l2 = pg.locator('[data-vmsub="logs"]').first(); if (await l2.count()) { await l2.click(); await pg.waitForTimeout(2500); }
    await pg.evaluate(() => window.scrollTo(0, 0));
    await pg.screenshot({ path: `${OUT}/vm-logs.png` }); console.log('  ✓ vm-logs.png (2x)');
    await ctx2.close();
}

// ---- 08/09/10 diagnostics: bus monitor, register probe, SunSpec scan -------------
await go('diagnostics', 1500);
await page.click('#diagToggleBtn'); await wait(4000);
await top(); await shot('guide/08-diagnostics-busmonitor.png');
await page.click('#diagToggleBtn').catch(() => {}); await wait(300);
const probe = page.locator('#probeBtn');
if (await probe.count()) {
    await page.locator('#probeAddr').fill('19000').catch(() => {});
    await probe.scrollIntoViewIfNeeded(); await probe.click(); await wait(2500);
    await page.locator('#probeResult').scrollIntoViewIfNeeded().catch(() => {});
    await page.evaluate(() => { const el = document.getElementById('probeDevice'); if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 120); });
    await wait(300); await shot('guide/09-diagnostics-probe.png');
}
const ss = page.locator('#ssPort');
if (await ss.count()) {
    await page.evaluate(() => { const el = document.getElementById('ssPort'); if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 160); });
    await page.fill('#ssHost', process.env.SUNSPEC_HOST || '127.0.0.1').catch(() => {});
    await page.fill('#ssPort', process.env.SUNSPEC_PORT || '6503').catch(() => {});
    await page.fill('#ssUnit', '1').catch(() => {});
    await page.click('#ssBtn').catch(() => {}); await wait(4000);
    // the scan result is the last thing on the page: scroll its container so
    // the whole model chain sits above the status bar
    await page.evaluate(() => {
        const r = document.getElementById('ssResult') || document.getElementById('ssHost');
        if (!r) return;
        r.scrollIntoView({ block: 'end' });
        let c = r.parentElement;
        while (c && !(['auto', 'scroll'].includes(getComputedStyle(c).overflowY) && c.scrollHeight > c.clientHeight)) c = c.parentElement;
        (c || document.scrollingElement).scrollTop += 120;
    });
    await wait(400);
    await shot('guide/10-diagnostics-sunspec.png');
}

// ---- 11..17 settings tabs ---------------------------------------------------------
await go('config', 1500);
for (const [tab, name] of [['mqtt', '11-settings-mqtt'], ['influxdb', '12-settings-influxdb'], ['backup', '13-settings-backup-snapshots'], ['security', '14-settings-security'], ['alerts', '16-settings-alerts'], ['general', '17-settings-general']]) {
    await page.click(`[data-cfgtab="${tab}"]`); await wait(1500); await top(); await shot(`guide/${name}.png`);
    if (tab === 'security') {
        await page.evaluate(() => document.getElementById('auditFilter')?.scrollIntoView({ block: 'start' }));
        await page.evaluate(() => window.scrollBy(0, -260));
        await wait(800); await shot('guide/15-settings-security-audit.png');
    }
}

// ---- 18 device wizard, step 1 ----------------------------------------------------------
await go('devices', 1200);
await page.locator('[data-action="openDeviceWizard"]').first().click(); await wait(1200);
await shot('guide/18-wizard-step1-connection.png');
await closeModals(); await wait(300);

// ---- 19 installation page ---------------------------------------------------------------
await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click(); await wait(3500);
await top(); await shot('guide/19-installation.png');

// ---- 20 unit overview, 21 unit commands -----------------------------------------------------
await openUnit('sunfield-u1', 'overview', 2500); await top(); await shot('guide/20-unit-overview.png');
await page.locator('#deviceWsTabs .config-main-tab[data-dtab="commands"]').click(); await wait(2500);
await top(); await shot('guide/21-unit-outputs-commands.png');

// ---- 22 rules ---------------------------------------------------------------------------------
await go('rules', 2500); await top(); await shot('guide/22-rules.png');

await br.close();
if (errs.length) console.log('page errors:', errs.slice(0, 5).join(' | '));
console.log('done');
