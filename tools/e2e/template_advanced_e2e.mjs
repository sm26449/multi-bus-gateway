// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Everything in a template, edited in the UI (3.91) — no external editor:
// calculated fields (checked against the map as you type), commands, display,
// scan recognition (tried on a real device), details and categories, a row's
// details dialog and the raw JSON. Saved, and read back as the server has it.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox node template_advanced_e2e.mjs
//
// Device: the Modbus TCP gateway sim (sims/bridge_sims.py) on 15502, unit 1:
// holding 0 = 5555, holding 1 = 6666.
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const CTR = process.env.CTR || 'mbg-ui-sandbox';
const APP_UID = process.env.APP_UID || '10001';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const TPL = 'e2e_adv_tpl';
const DEV = 'e2e-adv-dev';
const PORT = 15502;

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
const sleep = ms => new Promise(r => setTimeout(r, ms));
const docker = args => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
const inCtr = cmd => docker(['exec', '-u', APP_UID, CTR, 'sh', '-c', cmd]);
const tplNow = async () => (await api(`/api/device-templates/${TPL}`)).body?.device_template || {};
async function cleanup() {
  await del(`/api/devices/${DEV}`);
  await del(`/api/device-templates/${TPL}`);
  try { inCtr(`rm -rf /app/config/devices/${DEV}`); } catch { /* none */ }
}

await cleanup();
try { inCtr(`rm -f /tmp/br_sims.stop /tmp/br_sim_${PORT}.stop`); } catch { /* none */ }
docker(['cp', path.join(HERE, 'sims', 'bridge_sims.py'), `${CTR}:/tmp/bridge_sims.py`]);
docker(['exec', '-d', '-u', APP_UID, CTR, 'sh', '-c', `python3 /tmp/bridge_sims.py gateway ${PORT} > /tmp/br_${PORT}.log 2>&1`]);
await sleep(2500);

// a plain map, as the visual editor makes it; Advanced does the rest
const base = await post('/api/device-templates', { device_template: {
  id: TPL, name: 'E2E advanced', vendor: 'E2E', protocol: { transports: ['tcp'], byte_order: 'big' },
  poll_groups: { normal: { interval: 2 }, controls: { interval: 30 } },
  categories: { power: { label: 'Power', order: 1 } },
  registers: [
    { address: 0, name: 'power_l1', unit: 'W', data_type: 'uint16', category: 'power', poll_group: 'normal' },
    { address: 1, name: 'power_l2', unit: 'W', data_type: 'uint16', category: 'power', poll_group: 'normal' },
    { address: 2, name: 'limit_pct', unit: '%', data_type: 'uint16', category: 'power', poll_group: 'controls',
      writable: true, write_min: 0, write_max: 100 },
  ] } });
const devMade = await post('/api/devices', { id: DEV, template: TPL, enabled: true,
  connection: { protocol: 'tcp', host: '127.0.0.1', port: PORT, unit_id: 1 } });

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('console', m => { if (m.type() === 'error' && !/status of (4\d\d|502)/.test(m.text())) errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));
const adv = '#tplAdvPanel';
async function fill(sel, v) { const l = page.locator(sel); await l.fill(String(v)); await l.dispatchEvent('change'); }
async function tab(id) { await page.click(`.tpl-adv-tab[data-args='["${id}"]']`); }
async function checkBox() {
  await page.evaluate(() => { const b = document.getElementById('tplAdvCheck'); if (b) b.textContent = ''; });
  await page.waitForFunction(() => /No problems|refuse/.test(document.getElementById('tplAdvCheck')?.textContent || ''), null, { timeout: 8000 }).catch(() => {});
  return (await page.textContent('#tplAdvCheck')).replace(/\s+/g, ' ').trim();
}

try {
  check('the plain map and a device on the sim (API)', base.status === 200 && devMade.status === 200,
    JSON.stringify([base.body, devMade.body]).slice(0, 200));
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="templates"]');
  await page.click(`[data-tm-edit="${TPL}"]`);
  await page.waitForSelector('#devTplModal.active');
  await page.click('#tplAdv > summary');
  check('the editor has an Advanced section with six tabs', await page.locator('.tpl-adv-tab').count() === 6);

  // ═══ 1. a calculated field, checked against THIS map as it is typed ═════
  await page.click(`${adv} [data-action="_advAddCalc"]`);
  await fill(`${adv} input[aria-label="name"]`, 'power_active_total');
  await fill(`${adv} input[aria-label="unit"]`, 'W');
  const expr = page.locator('#advExpr');
  await expr.fill('power_l1 + powr_l2');
  await page.waitForFunction(() => /not a register/.test(document.getElementById('advExprCheck')?.textContent || ''), null, { timeout: 8000 }).catch(() => {});
  check('a typo in a formula is named before saving', /not a register or calculated field of this template: powr_l2/.test(await page.textContent('#advExprCheck')),
    await page.textContent('#advExprCheck'));
  await expr.fill('');
  await page.click(`${adv} .calc-chip:text-is("power_l1")`);
  await expr.press('End');
  await page.click(`${adv} .calc-chip.op:text-is("+")`);
  await page.click(`${adv} .calc-chip:text-is("power_l2")`);
  await page.waitForFunction(() => /Valid/.test(document.getElementById('advExprCheck')?.textContent || ''), null, { timeout: 8000 }).catch(() => {});
  const ok = (await page.textContent('#advExprCheck')).replace(/\s+/g, ' ');
  check('built from the chips, the formula is valid and says what it reads', /Valid · reads power_l1, power_l2/.test(ok), ok);
  await page.click(`${adv} .tpl-adv-form [data-action="_advEdit"]`);       // Done

  // ═══ 2. a command ═══════════════════════════════════════════════════════
  await tab('commands');
  await page.click(`${adv} [data-action="_advAddKey"]`);
  await fill(`${adv} input[aria-label="command name"]`, 'power_limit');
  await fill(`${adv} input[aria-label="label"]`, 'Power limit');
  await fill(`${adv} input[aria-label="min"]`, 0);
  await fill(`${adv} input[aria-label="max"]`, 100);
  await page.locator(`${adv} select[aria-label="register"]`).selectOption('limit_pct');
  await page.locator(`${adv} select[aria-label="register"]`).dispatchEvent('change');
  // verify: read limit_pct back
  const addVerify = page.locator(`${adv} .form-group:has(> label:text-is("Then check it took (verify)")) [data-action="_advAdd"]`);
  await addVerify.click();
  await page.locator(`${adv} .form-group:has(> label:text-is("Then check it took (verify)")) select[aria-label="read"]`).selectOption('limit_pct');
  await fill(`${adv} input[aria-label="tolerance"]`, 1);
  await page.locator(`${adv} select[aria-label="readback group"]`).selectOption('controls');
  let cb = await checkBox();
  check('the live check finds nothing wrong with the command', /No problems/.test(cb), cb);
  // a mistake shows at once: a write to a register that is not in the map
  await page.locator(`${adv} select[aria-label="register"]`).selectOption('');
  cb = await checkBox();
  check('…and a write to no register is refused before saving', /writes\[0\] register/.test(cb), cb);
  await page.locator(`${adv} select[aria-label="register"]`).selectOption('limit_pct');
  await page.click(`${adv} .tpl-adv-form [data-action="_advEdit"]`);

  // ═══ 3. display ════════════════════════════════════════════════════════
  await tab('display');
  await fill(`${adv} input[aria-label="unit label"]`, 'meter');
  await fill(`${adv} input[aria-label="unit label plural"]`, 'meters');
  await fill(`${adv} input[aria-label="glance"]`, 'power_active_total, power_l1');
  const addAlarm = page.locator(`${adv} .form-group:has(> label:text-is("Alarms")) [data-action="_advAdd"]`);
  await addAlarm.click();
  await page.locator(`${adv} select[aria-label="field"]`).first().selectOption('limit_pct');
  await page.locator(`${adv} select[aria-label="severity"]`).selectOption('danger');

  // ═══ 4. scan recognition, tried on the real device ═════════════════════
  await tab('identify');
  await page.click(`${adv} .form-group:has(> label:text-is("Registers that must read")) [data-action="_advAdd"]`);
  await fill(`${adv} input[aria-label="address"]`, 1);
  await fill(`${adv} input[aria-label="equals"]`, 6666);
  await page.selectOption('#advIdfDev', DEV);
  await page.click('#advIdfBtn');
  await page.waitForFunction(() => /recognised|does not match/.test(document.getElementById('advIdfResult')?.textContent || ''), null, { timeout: 15000 });
  check('identify, tried on the running device, recognises it (holding 1 = 6666)',
    /is recognised as this template/.test(await page.textContent('#advIdfResult')), await page.textContent('#advIdfResult'));
  await fill(`${adv} input[aria-label="equals"]`, 1234);
  await page.click('#advIdfBtn');
  await page.waitForFunction(() => /does not match/.test(document.getElementById('advIdfResult')?.textContent || ''), null, { timeout: 15000 }).catch(() => {});
  check('…and a wrong value does not match', /does not match/.test(await page.textContent('#advIdfResult')));
  await fill(`${adv} input[aria-label="equals"]`, 6666);

  // ═══ 5. details & categories ═══════════════════════════════════════════
  await tab('details');
  await fill(`${adv} input[aria-label="version"]`, '1.1.0');
  await fill(`${adv} input[aria-label="source document"]`, 'E2E manual p. 3');
  await fill(`${adv} input[aria-label="category id"]`, 'power_meas');
  const rowCat = await page.locator('#devTplBody tbody tr').first().locator('[data-f="category"]').inputValue();
  check('renaming a category renames it on its rows', rowCat === 'power_meas', rowCat);
  await page.click('#tplAdv > summary').catch(() => {});
  if (!(await page.isVisible('#tplAdvPanel'))) await page.click('#tplAdv > summary');

  // ═══ 6. a row's details dialog ═════════════════════════════════════════
  await page.locator('#devTplBody tbody tr').first().locator('[data-action="tplRowExtras"]').click();
  await page.waitForSelector('#tplRowModal.active');
  await fill('#tplRowBody input[aria-label="nan"]', 'true');
  await page.locator('#tplRowBody label:has-text("Counter that only grows") input').check();
  await page.click('#tplRowBody [data-action="_advAddKey"]');
  await fill('#tplRowBody input[aria-label="output name"]', 'site_power_l1');
  await page.locator('#tplRowBody select[aria-label="state class"]').selectOption('measurement');
  await page.click('#tplRowModal .modal-footer [data-action="tplRowDone"]');
  await page.waitForSelector('#tplRowModal', { state: 'hidden' });
  const rowBtnColor = await page.locator('#devTplBody tbody tr').first().locator('[data-action="tplRowExtras"]').getAttribute('style');
  check('a row with details carries a marked button', /accent/.test(rowBtnColor || ''), rowBtnColor);

  // ═══ 7. raw JSON: check a bad edit, apply a good one ═══════════════════
  if (!(await page.isVisible('#tplAdvPanel'))) await page.click('#tplAdv > summary');
  await tab('json');
  const json = await page.inputValue('#advJson');
  check('the raw JSON carries everything set above',
    ['"power_active_total"', '"power_limit"', '"unit_label": "meter"', '"equals": 6666', '"version": "1.1.0"', '"site_power_l1": "sum"', '"monotonic": true']
      .every(s => json.includes(s)), json.slice(0, 300));
  await page.fill('#advJson', json.replace('"state_class": "measurement"', '"state_class": "gauge"'));
  await page.click(`${adv} [data-action="_advJsonCheck"]`);
  await page.waitForSelector('#advJsonResult .calc-preview');
  check('Check names a wrong value in the JSON', /state_class must be one of/.test(await page.textContent('#advJsonResult')),
    await page.textContent('#advJsonResult'));
  await page.fill('#advJson', json.replace('"version": "1.1.0"', '"version": "1.2.0"'));
  await page.click(`${adv} [data-action="_advJsonApply"]`);
  await page.waitForTimeout(400);

  // ═══ 8. saved, as the server keeps it ══════════════════════════════════
  await page.click('#devTplSaveBtn');
  await page.waitForSelector('#devTplModal', { state: 'hidden' }).catch(() => {});
  const fb = await page.locator('#tplErrors').textContent().catch(() => '');
  const t = await tplNow();
  const r0 = (t.registers || []).find(r => r.name === 'power_l1') || {};
  check('saved: version from the raw JSON', t.version === '1.2.0', `${t.version} ${fb}`);
  check('saved: the calculated field', (t.calculated || []).some(c => c.name === 'power_active_total' && /power_l1\s*\+\s*power_l2/.test(c.expr)),
    JSON.stringify(t.calculated));
  const pl = (t.commands || {}).power_limit || {};
  check('saved: the command with its bounds, write, verify and re-read group',
    pl.params?.value?.min === 0 && pl.params?.value?.max === 100 && pl.writes?.[0]?.register === 'limit_pct'
    && pl.verify?.[0]?.read === 'limit_pct' && pl.readback_group === 'controls', JSON.stringify(pl));
  check('saved: display', t.display?.unit_label === 'meter' && (t.display?.glance || []).join() === 'power_active_total,power_l1'
    && t.display?.alarms?.[0]?.severity === 'danger', JSON.stringify(t.display));
  check('saved: identify', t.identify?.registers?.[0]?.address === 1 && t.identify?.registers?.[0]?.equals === 6666, JSON.stringify(t.identify));
  check('saved: the row details (nan, growing counter, a total, HA state class)',
    r0.nan === true && r0.monotonic === true && r0.aggregates?.site_power_l1 === 'sum' && r0.state_class === 'measurement', JSON.stringify(r0));
  check('saved: categories renamed on the map', !!t.categories?.power_meas && r0.category === 'power_meas', JSON.stringify(t.categories));

  // ═══ 9. phone width: the Advanced section fits ═════════════════════════
  await page.setViewportSize({ width: 390, height: 844 });
  await page.click(`[data-tm-edit="${TPL}"]`);
  await page.waitForSelector('#devTplModal.active');
  await page.click('#tplAdv > summary');
  await tab('commands');
  await page.click(`${adv} [data-action="_advEdit"]`);
  await page.waitForTimeout(300);
  const wide = await page.evaluate(() => [...document.querySelectorAll('#tplAdv, #tplAdv *')]
    .filter(e => e.getBoundingClientRect().right > document.querySelector('#devTplModal .modal-content').getBoundingClientRect().right + 2)
    .map(e => e.className || e.tagName).slice(0, 4));
  check('at 390 px nothing in Advanced overflows the dialog', !wide.length, wide.join(' | '));
  await page.setViewportSize({ width: 1440, height: 950 });

  check('no console errors', errs.length === 0, errs.slice(0, 3).join(' | '));
} catch (e) {
  check('script ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
  await cleanup();
  try { inCtr(`touch /tmp/br_sim_${PORT}.stop`); } catch { /* gone */ }
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
