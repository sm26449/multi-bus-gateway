/* Full UI sweep: every page and state, screenshot + console errors + raw i18n
 * keys + horizontal overflow. Output: /tmp/audit/*.png and report.json */
import { chromium } from 'playwright';
import fs from 'fs';
const B = 'http://localhost:18090', OUT = '/tmp/audit';
const report = [];
const br = await chromium.launch({ executablePath: '/root/.cache/ms-playwright/chromium-1148/chrome-linux/chrome' });

async function snap(p, name, opts = {}) {
  await p.waitForTimeout(opts.wait ?? 1500);
  const info = await p.evaluate(() => {
    const raw = [...document.querySelectorAll('body *')]
      .map(e => (e.childElementCount === 0 ? (e.textContent || '').trim() : ''))
      .filter(t => /^[a-z]+(\.[a-zA-Z0-9_]+){1,}$/.test(t) && t.length < 60);   // looks like an i18n key
    const overflow = document.documentElement.scrollWidth > document.documentElement.clientWidth + 2;
    const empties = [...document.querySelectorAll('.settings-card-body, .card-body')]
      .filter(e => e.offsetParent && (e.innerText || '').trim().length === 0).length;
    return { rawKeys: [...new Set(raw)].slice(0, 8), overflow, emptyCards: empties,
             title: document.title };
  });
  await p.screenshot({ path: `${OUT}/${name}.png`, fullPage: !!opts.full });
  report.push({ name, ...info, errors: p._errs.splice(0) });
  console.log(`shot ${name}${info.overflow ? ' [OVERFLOW]' : ''}${info.rawKeys.length ? ' [RAW i18n: ' + info.rawKeys.join(',') + ']' : ''}`);
}
async function fresh(viewport = { width: 1440, height: 1000 }, dark = false) {
  const p = await br.newPage({ viewport, colorScheme: dark ? 'dark' : 'light' });
  p.setDefaultTimeout(15000); p._errs = [];
  p.on('console', m => { if (m.type() === 'error') p._errs.push(m.text().slice(0, 160)); });
  p.on('pageerror', e => p._errs.push(String(e).slice(0, 160)));
  await p.goto(B + '/'); await p.waitForLoadState('networkidle');
  const later = p.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  return p;
}
const go = async (p, page) => { await p.click(`[data-page="${page}"]`); };
const toList = async (p) => {
  await p.evaluate(() => { try { app.closeDeviceDetail(); } catch (e) {} try { app.closeEndpointDetail(); } catch (e) {} });
  await go(p, 'devices'); await p.waitForTimeout(1000);
};
const openInst = async (p) => { await toList(p); await p.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click(); await p.waitForTimeout(2500); };

let p = await fresh();
// ── top-level pages ──
for (const pg of ['dashboard', 'devices', 'templates', 'status', 'config', 'vmeters', 'diagnostics']) {
  try { await go(p, pg); await snap(p, `10-page-${pg}`, { full: true }); } catch (e) { console.log('skip', pg, e.message.slice(0, 60)); }
}
// ── devices: installation + unit tabs ──
await go(p, 'devices'); await p.waitForTimeout(1000);
await p.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
await snap(p, '20-installation', { full: true, wait: 3500 });
await p.locator('[data-group-units] tr[data-unit="pv-u1"] button[data-action="openDeviceDetail"]').first().click();
for (const tab of ['overview', 'edit', 'outputs', 'measurements', 'calculated', 'logs', 'monitor', 'history', 'energy']) {
  try {
    await p.locator(`#deviceWsTabs .config-main-tab[data-dtab="${tab}"]`).click();
    await snap(p, `30-unit-${tab}`, { full: true, wait: 2500 });
  } catch (e) { console.log('skip tab', tab, e.message.slice(0, 60)); }
}
// site unit (single-unit group, different template)
await openInst(p);
await p.locator('[data-group-units] tr[data-unit="pv-site"] button[data-action="openDeviceDetail"]').first().click();
await snap(p, '31-site-unit-overview', { full: true, wait: 2500 });
// ── modals ──
await openInst(p);
await p.locator('button[data-action="openGroupModal"]').first().click(); await snap(p, '40-modal-add-group');
await p.evaluate(() => app.closeModal("endpointModal")); await p.waitForTimeout(400);
await p.locator('[data-group="inverters"] button[data-action="openSourceModal"]').first().click(); await snap(p, '41-modal-add-source');
await p.evaluate(() => app.closeModal("endpointModal")); await p.waitForTimeout(400);
await p.locator('[data-group="inverters"] button[data-action="openGroupModal"]').first().click(); await snap(p, '42-modal-edit-group');
await p.evaluate(() => app.closeModal("endpointModal")); await p.waitForTimeout(400);
await p.locator('[data-endpoint-page] button[data-action="openEndpointModal"]').click(); await snap(p, '43-modal-edit-installation');
await p.evaluate(() => app.closeModal("endpointModal")); await p.waitForTimeout(400);
await p.locator('[data-endpoint-page] button[data-action="testEndpointUi"]').click(); await snap(p, '44-test-units-result', { full: true, wait: 6000 });
// ── the installation wizard, every step + a validation failure ──
await toList(p);
await p.locator('button:has-text("Add Installation")').click(); await snap(p, '45-wizard-step1-empty');
await p.locator('#plantWizardModal button:has-text("Next")').click(); await snap(p, '46-wizard-step1-validation');
await p.fill('#plantWizardModal input[name="id"], #plantWizardModal #plantWizId', 'audit').catch(() => {});
await p.evaluate(() => { const i = document.querySelector('#plantWizardModal input'); if (i && !i.value) { i.value = 'audit'; i.dispatchEvent(new Event('input')); } });
await p.locator('#plantWizardModal button:has-text("Next")').click(); await snap(p, '47-wizard-step2', { full: true });
await p.locator('#plantWizardModal button:has-text("Next")').click(); await snap(p, '48-wizard-step3', { full: true });
await p.locator('#plantWizardModal button:has-text("Next")').click(); await snap(p, '49-wizard-step4', { full: true });
await p.evaluate(() => app.closeModal("plantWizardModal")); await p.waitForTimeout(300);
// ── device wizard (the 3-step one) for comparison ──
await toList(p);
await p.locator('button:has-text("Add Device")').click(); await snap(p, '50-device-wizard-step1');
await p.evaluate(() => app.closeModal("deviceWizardModal")); await p.waitForTimeout(300);
// ── dark theme + narrow viewport ──
await p.close();
p = await fresh({ width: 1440, height: 1000 }, true);
await go(p, 'devices'); await p.waitForTimeout(1000);
await p.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
await snap(p, '60-installation-dark', { full: true, wait: 3000 });
await p.close();
p = await fresh({ width: 390, height: 844 });
await go(p, 'devices'); await snap(p, '70-mobile-devices', { full: true });
await p.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click();
await snap(p, '71-mobile-installation', { full: true, wait: 3000 });
await p.close();
await br.close();
fs.writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
console.log(`\n${report.length} screens. errors on: ${report.filter(r => r.errors.length).map(r => r.name).join(', ') || 'none'}`);
