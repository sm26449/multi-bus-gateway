// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// What a copy/paste brings: a formula or a topic with an invisible or a
// look-alike character is named under the field as it is typed, and the
// server refuses to save it.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node paste_check_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const TPL = 'e2e_paste';
const U = n => String.fromCodePoint(n);
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
const tpl = topic => ({ device_template: { id: TPL, name: 'E2E paste', vendor: 'E2E', protocol: { transports: ['mqtt'] },
  registers: [{ address: 1, name: 'soc', unit: '%', data_type: 'float', topic, json_path: 'value' }] } });

await api(`/api/device-templates/${TPL}`, { method: 'DELETE' });
const bad = await api('/api/device-templates', { method: 'POST', body: JSON.stringify(tpl('a/soc' + U(0x200B))) });
const made = await api('/api/device-templates', { method: 'POST', body: JSON.stringify(tpl('a/soc')) });

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
try {
  check('the server refuses a topic with an invisible character', bad.status >= 400
        && JSON.stringify(bad.body).includes('invisible'), JSON.stringify(bad.body).slice(0, 200));
  check('a clean template saves', made.status === 200);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="templates"]');
  await page.click(`[data-tm-edit="${TPL}"]`);
  await page.waitForSelector('#devTplModal.active');
  const cell = page.locator('#devTplBody tr[data-idx="0"] input[data-f="topic"]');
  await cell.fill('a/soc' + U(0x00A0) + 'x');
  const issue = page.locator('#devTplBody tr[data-idx="0"] .paste-issue');
  check('the field says which character, at once', (await issue.textContent() || '').includes('U+00A0'),
        await issue.textContent().catch(() => ''));
  await cell.fill('a/soc');
  check('and the note goes when it is fixed', await issue.count() === 0);
  const f = await page.evaluate(([m, c]) => [window.app._formulaIssue('a ' + m + ' b'), window.app._formulaIssue('soc_' + c),
                                             window.app._formulaIssue('a +\n b')], [U(0x2212), U(0x0430)]);
  check('a typographic minus: type "-"', f[0].includes('“-”'), f[0]);
  check('a Cyrillic look-alike is not a plain letter', f[1].includes('U+0430'), f[1]);
  check('line breaks are fine', f[2] === '');
  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await api(`/api/device-templates/${TPL}`, { method: 'DELETE' });
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
