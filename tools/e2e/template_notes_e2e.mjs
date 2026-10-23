// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Notes for users: what a template says before anyone relies on it. Written
// in the Advanced editor, shown where the template is picked (wizard), at the
// top of every device made from it, and as a mark on the command it names.
// A device with no energy counter shows no Energy tab.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node template_notes_e2e.mjs
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const APP_UID = process.env.APP_UID || '10001';
const TPL = 'e2e_notes_tpl';
const DEV = 'e2e-notes-dev';

const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (p, opts = {}) => {
  const r = await fetch(BASE + p, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  return { status: r.status, body };
};
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });
const del = p => api(p, { method: 'DELETE' });
const inCtr = cmd => execFileSync('docker', ['exec', '-u', APP_UID, CTR, 'sh', '-c', cmd], { encoding: 'utf8' });
const tplNow = async () => (await api(`/api/device-templates/${TPL}`)).body?.device_template || {};
async function cleanup() {
  await del(`/api/devices/${DEV}`);
  await del(`/api/device-templates/${TPL}`);
  try { inCtr(`rm -rf /app/config/devices/${DEV}`); } catch { /* none */ }
}

await cleanup();
const made = await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E notes', vendor: 'E2E', protocol: { transports: ['tcp'] },
  poll_groups: { normal: { interval: 5 } },
  registers: [
    { address: 0, name: 'flow', label: 'Flow', unit: 'l/min', data_type: 'uint16' },
    { address: 1, name: 'quiet_reg', label: 'Quiet', data_type: 'uint16', writable: true, write_min: 0, write_max: 1 },
  ],
  commands: { quiet: { label: 'Quiet', params: { value: { min: 0, max: 1 } },
                       writes: [{ register: 'quiet_reg', value: '${value}' }] } },
  notes: [
    { level: 'warning', text: 'Quiet has no effect on the X model.', fields: ['quiet'] },
    { level: 'info', text: 'Values appear after the first connect.' },
  ],
} });
const dev = await post('/api/devices', { id: DEV, name: 'E2E notes', enabled: false, template: TPL,
  connection: { protocol: 'tcp', host: '127.0.0.1', port: 15599, unit_id: 1 } });

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
try {
  check('a template with notes and a device made from it (API)', made.status === 200 && dev.status === 200,
    JSON.stringify([made.body, dev.body]).slice(0, 300));
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ═══ 1. the device page ═══════════════════════════════════════════════
  await page.click('[data-page="devices"]'); await page.waitForTimeout(800);
  await page.evaluate(id => window.app.openDeviceDetail(id), DEV);
  await page.waitForSelector('#deviceWsTabs');
  const notes = page.locator('#deviceDetailView .tpl-note');
  check('the device page shows both notes', await notes.count() === 2, String(await notes.count()));
  check('the warning is marked as one and names its command',
    await page.locator('#deviceDetailView .tpl-note.warn').count() === 1
    && /quiet/.test(await page.locator('#deviceDetailView .tpl-note.warn .tpl-note-fields').textContent()));
  check('no energy counter → no Energy tab', await page.locator('#deviceWsTabs [data-dtab="energy"]').count() === 0);
  await openDeviceTab(page, 'commands');
  await page.waitForSelector('#deviceDetailView .cmd-card[data-cmd="quiet"]');
  const flag = page.locator('#deviceDetailView .cmd-card[data-cmd="quiet"] .sink-pill.warn:has(.bi-exclamation-triangle)');
  check('the command a warning names carries a mark with the note', await flag.count() === 1
    && /no effect/.test(await flag.getAttribute('title') || ''), await flag.count());

  // ═══ 2. the wizard, where the template is picked ═══════════════════════
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#deviceWizardModal.active');
  await page.evaluate(tpl => { const w = window.app._devWiz; w.data.protocol = 'tcp'; w.step = 2; w.data.template = tpl; window.app._devWizRender(); }, TPL);
  await page.waitForSelector('#devWizTplList');
  check('the template list flags a template with a warning',
    await page.locator(`#devWizTplList .tpl-pick[data-tpl="${TPL}"] .tpl-note-flag`).count() === 1);
  check('picking it shows its notes', await page.locator('#deviceWizardModal .tpl-note').count() === 2);
  await page.locator('#deviceWizardModal .modal-close').first().click().catch(() => {});
  await page.waitForTimeout(300);

  // ═══ 3. written in the Advanced editor ═════════════════════════════════
  await page.click('[data-page="templates"]');
  await page.click(`[data-tm-edit="${TPL}"]`);
  await page.waitForSelector('#devTplModal.active');
  await page.click('#tplAdv > summary');
  await page.click(`.tpl-adv-tab[data-args='["notes"]']`);
  const panel = '#tplAdvPanel';
  check('the Notes tab lists the two notes', await page.locator(`${panel} textarea[aria-label="Note text"]`).count() === 2);
  await page.click(`${panel} [data-action="_advAdd"]`);
  const i = 2;
  await page.locator(`${panel} select[aria-label="note level"]`).nth(i).selectOption('warning');
  await page.locator(`${panel} textarea[aria-label="Note text"]`).nth(i).fill('Flow reads 0 on the X model.');
  await page.locator(`${panel} textarea[aria-label="Note text"]`).nth(i).dispatchEvent('change');
  const f = page.locator(`${panel} input[aria-label="note fields"]`).nth(i);
  await f.fill('flow'); await f.dispatchEvent('change');
  const l = page.locator(`${panel} input[aria-label="note link"]`).nth(i);
  await l.fill('https://example.org/x-model'); await l.dispatchEvent('change');
  await page.click('#devTplSaveBtn');
  await page.waitForSelector('#devTplModal', { state: 'hidden' }).catch(() => {});
  const saved = (await tplNow()).notes || [];
  const n3 = saved[2] || {};
  check('saved: the note written in the editor', saved.length === 3 && n3.level === 'warning'
    && n3.text === 'Flow reads 0 on the X model.' && (n3.fields || []).join() === 'flow' && n3.link === 'https://example.org/x-model',
    JSON.stringify(saved));

  // a note naming something the template does not have is refused by name
  const bad = await post('/api/device-templates/check', { device_template: { ...(await tplNow()), notes: [{ text: 'x', fields: ['nope'] }] } });
  check('a note about a field the template lacks is named', /fields not in this template: nope/.test(JSON.stringify(bad.body)),
    JSON.stringify(bad.body).slice(0, 200));
  check('no page errors', errors.length === 0, errors.join(' | '));
} catch (e) {
  check('suite ran to the end', false, e.stack || String(e));
} finally {
  await browser.close();
  await cleanup();
}
const ok = results.filter(r => r.ok).length;
console.log(`\n${ok}/${results.length} passed`);
process.exit(ok === results.length ? 0 : 1);
