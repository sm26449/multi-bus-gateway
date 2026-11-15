/* vbms_crud_e2e (ADR-0045 L1): the Virtual BMS page's add/edit/delete UI against a real gateway.
 * Covers what needs no CAN interface — the Add modal wiring (profiles/sources/ports loaded, the
 * "no CAN interfaces" message in the loopback fixture) and the delete flow. The create-with-a-real-
 * port flow is the L3 e2e (a vcan fixture); the create logic itself is covered by the TestClient
 * route tests.
 *
 *   tools/e2e/fixture_up.sh vbms 18091
 *   MBG_URL=http://127.0.0.1:18091 CHROMIUM_PATH=<chrome> node vbms_crud_e2e.mjs
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18091';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  const dlg = await watchDialogs(page);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.goto(BASE + '/#/vbms'); await page.waitForTimeout(500);

  // the page now has an Add button (new in L1)
  const addBtn = page.locator('[data-action="openVbmsModal"]');
  check('the page offers "Add Virtual BMS"', await addBtn.count() >= 1);

  // open the Add modal → it loads the pickers from the API
  await addBtn.first().click();
  await page.waitForSelector('#vbmsModal.active', { timeout: 10000 });
  await page.waitForFunction(() => !!document.querySelector('#vbfProfile'), { timeout: 10000 }).catch(() => {});
  check('the modal loads emulation profiles', await page.evaluate(() => {
    const sel = document.getElementById('vbfProfile');
    return !!sel && [...sel.options].some(o => /pylontech/i.test(o.value));
  }));
  check('the port picker honestly says there is no CAN interface here', await page.evaluate(() =>
    /no CAN interfaces|nu s-au g/i.test(document.getElementById('vbmsModalBody').textContent)));
  check('the form has the limit fields', await page.evaluate(() =>
    !!document.getElementById('vbfCcl') && !!document.getElementById('vbfDcl')));
  check('the form offers the optional-detail-frames toggle (L2)', await page.evaluate(() =>
    !!document.getElementById('vbfSendOptional') && document.getElementById('vbfSendOptional').checked));
  // close it
  await page.click('#vbmsModal [data-action="closeModal"]');
  await page.waitForSelector('#vbmsModal.active', { state: 'detached', timeout: 5000 }).catch(() => {});

  // the aggregation preview expander (L2): opens, fetches /preview, renders
  const card = page.locator('.settings-card[data-vbms="deye-vbms"]');
  await card.waitFor({ timeout: 10000 });
  await card.locator('[data-action="toggleVbmsPreview"]').click();
  await page.waitForFunction(() => {
    const el = document.querySelector('.vbms-preview[data-vbms-pv="deye-vbms"]');
    return el && !el.hidden && !/Loading/i.test(el.textContent);
  }, { timeout: 10000 }).catch(() => {});
  check('the aggregation preview opens and reports the frames', await page.evaluate(() => {
    const el = document.querySelector('.vbms-preview[data-vbms-pv="deye-vbms"]');
    return el && !el.hidden && /frames|cadre|packs|pachete/i.test(el.textContent);
  }));
  // the page re-renders its cards every 5 s: an aggregation the user opened stays open,
  // and its button says it can be hidden (VBMS-UI-1)
  await page.waitForTimeout(6500);
  check('the opened aggregation survives the 5 s refresh', await page.evaluate(() => {
    const el = document.querySelector('.vbms-preview[data-vbms-pv="deye-vbms"]');
    const btn = document.querySelector('[data-vbms-pvbtn="deye-vbms"]');
    return el && !el.hidden && /frames|cadre|packs|pachete/i.test(el.textContent)
      && btn && btn.getAttribute('aria-expanded') === 'true';
  }));

  // arming walks the guided prerequisites (ADR-0046): a danger confirm, then "Enable Modbus
  // writes?" and "Enable Transmit on can0?". In this loopback fixture can0 is not a real CAN
  // interface, so the can_transmit fix cannot succeed and the instance correctly stays disarmed —
  // proving arming is gated on a transmit-capable port, not silently "armed · not transmitting".
  dlg.answer = true;                                   // confirm the danger + every "Enable now"
  await card.locator('button[data-action="setVbmsArmed"]').first().click();
  await page.waitForTimeout(2000);                     // let the prereq dialogs + fix attempts run
  check('arming asks the Modbus-writes prerequisite', dlg.seen.some(d => /writes|Modbus|scrieri/i.test((d.title || '') + (d.message || ''))), dlg.seen.map(d => d.title));
  check('arming asks the CAN-transmit prerequisite', dlg.seen.some(d => /Transmit|Transmite/i.test(d.title || '')), dlg.seen.map(d => d.title));
  const armed = await page.evaluate(() => fetch('/api/vbms').then(r => r.json()));
  check('with no real CAN port, the instance stays disarmed', armed['deye-vbms'] && armed['deye-vbms'].armed === false, armed['deye-vbms']);

  // delete the pre-configured instance (needs no port) → confirm → card gone → empty state
  dlg.answer = true;
  await card.locator('[data-action="deleteVbms"]').click();
  const delDlg = dlg.seen.find(d => /Delete|Ștergi/.test(d.title || ''));
  check('deleting asks to confirm', !!delDlg, dlg.seen.map(d => d.title));
  await page.waitForFunction(() => !document.querySelector('.settings-card[data-vbms="deye-vbms"]'), { timeout: 10000 }).catch(() => {});
  check('the deleted instance is gone from the page', await page.locator('.settings-card[data-vbms="deye-vbms"]').count() === 0);
  const gone = await page.evaluate(() => fetch('/api/vbms').then(r => r.json()));
  check('API: the instance is gone', !gone['deye-vbms'], gone);
  check('the page shows the empty state', await page.locator('[data-i18n="vbms.noneTitle"], .empty-state').count() >= 1);
  check('no native confirm/alert was used', dlg.native === 0, dlg.native);
} finally {
  await browser.close();
}

const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
