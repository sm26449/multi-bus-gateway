/* vbms_create_e2e (ADR-0045 L3): the full create-from-scratch flow against REAL CAN ports. The
 * fixture gives the container two vcan interfaces (MBG_FX_VCAN=1) and two battery devices, no
 * pre-configured Virtual BMS. Covers: the Add modal's port picker shows the vcan ports; create on
 * vcan0 → card appears; the port is then Reserved and a second create there is refused while vcan1
 * stays available (port-busy filtering); edit a limit; delete.
 *
 *   MBG_FX_VCAN=1 tools/e2e/fixture_up.sh vbmsdev 18092
 *   MBG_URL=http://127.0.0.1:18092 CHROMIUM_PATH=<chrome> node vbms_create_e2e.mjs
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18092';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

const fillForm = async (page, { id, port, packs }) => {
  if (id != null) await page.fill('#vbfId', id);
  await page.selectOption('#vbfProfile', { label: /pylontech/i }).catch(async () => {
    await page.selectOption('#vbfProfile', { index: 0 });
  });
  await page.selectOption('#vbfPort', port);
  for (const p of packs) await page.check(`#vbfPacks input[value="${p}"]`);
};

try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  const dlg = await watchDialogs(page);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.goto(BASE + '/#/vbms'); await page.waitForTimeout(500);

  // the API sees the two vcan ports and the two battery sources
  const ports0 = await page.evaluate(() => fetch('/api/vbms/ports').then(r => r.json()).then(d => d.ports));
  check('the vcan ports are available to the picker', ['vcan0', 'vcan1'].every(n => ports0.some(p => p.name === n && p.availability !== 'reserved')), ports0.map(p => p.name));
  const sources = await page.evaluate(() => fetch('/api/vbms/sources').then(r => r.json()).then(d => d.sources));
  check('the battery devices are offered as packs', ['bat-a', 'bat-b'].every(id => sources.some(s => s.id === id)), sources);

  // CREATE bank-1 on vcan0
  await page.click('[data-action="openVbmsModal"]');
  await page.waitForSelector('#vbmsModal.active'); await page.waitForFunction(() => !!document.querySelector('#vbfPort'));
  await fillForm(page, { id: 'bank-1', port: 'vcan0', packs: ['bat-a', 'bat-b'] });
  await page.click('[data-action="saveVbmsModal"]');
  await page.waitForFunction(() => !!document.querySelector('.settings-card[data-vbms="bank-1"]'), { timeout: 10000 }).catch(() => {});
  check('created bank-1 shows a card', await page.locator('.settings-card[data-vbms="bank-1"]').count() === 1);
  let st = await page.evaluate(() => fetch('/api/vbms').then(r => r.json()));
  check('API: bank-1 is on vcan0', st['bank-1'] && st['bank-1'].channel === 'vcan0', st['bank-1']);

  // the picker now marks vcan0 reserved; a second create there is refused; vcan1 stays available
  await page.click('[data-action="openVbmsModal"]');
  await page.waitForSelector('#vbmsModal.active'); await page.waitForFunction(() => !!document.querySelector('#vbfPort'));
  check('vcan0 is now Reserved in the picker', await page.evaluate(() => {
    const o = [...document.querySelectorAll('#vbfPort option')].find(o => o.value === 'vcan0');
    return o && o.disabled;
  }));
  check('vcan1 is still selectable', await page.evaluate(() => {
    const o = [...document.querySelectorAll('#vbfPort option')].find(o => o.value === 'vcan1');
    return o && !o.disabled;
  }));
  await fillForm(page, { id: 'bank-2', port: 'vcan1', packs: ['bat-a'] });
  await page.click('[data-action="saveVbmsModal"]');
  await page.waitForFunction(() => !!document.querySelector('.settings-card[data-vbms="bank-2"]'), { timeout: 10000 }).catch(() => {});
  check('created bank-2 on vcan1', await page.locator('.settings-card[data-vbms="bank-2"]').count() === 1);

  // EDIT bank-1's charge limit
  await page.locator('.settings-card[data-vbms="bank-1"] [data-action="openVbmsModal"]').click();
  await page.waitForSelector('#vbmsModal.active'); await page.waitForFunction(() => !!document.querySelector('#vbfCcl'));
  await page.fill('#vbfCcl', '40');
  await page.click('[data-action="saveVbmsModal"]');
  await page.waitForTimeout(800);
  const cfg = await page.evaluate(() => fetch('/api/vbms/bank-1').then(r => r.json()));
  check('edit persisted the new charge limit', (cfg.bounds || {}).charge_current_limit && cfg.bounds.charge_current_limit[1] === 40, cfg.bounds);

  // DELETE bank-2
  dlg.answer = true;
  await page.locator('.settings-card[data-vbms="bank-2"] [data-action="deleteVbms"]').click();
  await page.waitForFunction(() => !document.querySelector('.settings-card[data-vbms="bank-2"]'), { timeout: 10000 }).catch(() => {});
  st = await page.evaluate(() => fetch('/api/vbms').then(r => r.json()));
  check('bank-2 deleted, bank-1 remains', !st['bank-2'] && !!st['bank-1'], st);

  // Settings → CAN (ADR-0046 L3): the tab lists the CAN interfaces with their mode
  await page.goto(BASE + '/#/config'); await page.waitForTimeout(300);
  await page.click('[data-cfgtab="can"]'); await page.waitForTimeout(600);
  check('Settings → CAN lists the vcan interfaces with their mode', await page.evaluate(() => {
    const names = [...document.querySelectorAll('#cfgCanContent .settings-card[data-can]')].map(c => c.getAttribute('data-can'));
    return names.includes('vcan0') && names.includes('vcan1') && /Transmit|Transmite/.test(document.getElementById('cfgCanContent').textContent);
  }));
  check('no native confirm/alert was used', dlg.native === 0, dlg.native);
} finally {
  await browser.close();
}

const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
