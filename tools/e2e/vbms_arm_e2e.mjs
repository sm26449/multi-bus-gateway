/* vbms_arm_e2e (ADR-0037): the Virtual BMS page lists the configured instance and lets an
 * operator ARM it. Arming asks for a danger confirm that warns it will transmit; the throwaway
 * gateway has no can0, so the arm gate (ADR-0046) refuses with its guided prerequisite dialog
 * and nothing is armed. Value trace: config virtual_bms → GET /api/vbms → card →
 * POST /api/vbms/<id>/arm (409 + prerequisites) → the pill + the API agree.
 *
 * Arming is gated on security.allow_writes, so bring the fixture up with --writes.
 *   tools/e2e/fixture_up.sh vbms 18091 --writes
 *   MBG_URL=http://127.0.0.1:18091 CHROMIUM_PATH=<chrome> node vbms_arm_e2e.mjs
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18091';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

const pill = (page) => page.evaluate(() => {
  const c = document.querySelector('.settings-card[data-vbms="deye-vbms"] .sink-pill');
  return c ? c.textContent.trim() : null;
});
const apiVbms = (page) => page.evaluate(() => fetch('/api/vbms').then(r => r.json()));

try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  const dlg = await watchDialogs(page);                 // intercept the confirm dialog before load
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});

  // the Virtual BMS page is now the real page (not the "coming soon" placeholder)
  await page.goto(BASE + '/#/vbms'); await page.waitForTimeout(500);
  const card = page.locator('.settings-card[data-vbms="deye-vbms"]');
  await card.waitFor({ timeout: 15000 });
  check('the configured Virtual BMS shows a card', await card.count() === 1);
  check('no "coming soon" placeholder remains', await page.locator('[data-i18n="vbms.soonTitle"]').count() === 0);
  check('it starts Disarmed', /Disarmed|Dezarmat/.test(await pill(page)), await pill(page));
  check('it names its CAN interface', await card.locator('code:text-is("can0")').count() === 1);
  check('it names its source packs', await card.locator('.dev-chip:text-is("seplos-adr1")').count() === 1
    && await card.locator('.dev-chip:text-is("seplos-adr2")').count() === 1);
  check('a disarmed instance offers Arm', await card.locator('button[data-action="setVbmsArmed"]:has-text("Arm"), button[data-action="setVbmsArmed"]:has-text("Armează")').count() === 1);

  // ARM → a danger confirm warns it will transmit → confirmed; the throwaway gateway has no
  // can0, so the arm gate (ADR-0046) refuses with its guided prerequisite dialog naming the
  // port — the operator cancels it — and nothing is armed. (Until ADR-0046 an arm without the
  // port landed on "Armed · not transmitting"; arming with no transmit path is now refused.)
  dlg.answer = false;
  dlg.answers.push(true, false);                        // the arm confirm: yes; the prerequisite: cancel
  await card.locator('button[data-action="setVbmsArmed"]').first().click();
  await page.waitForFunction(() => document.querySelectorAll('#appDialog.active').length === 0, null, { timeout: 12000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const armDlg = dlg.seen.find(d => /^Arm|Armezi/.test(d.title || ''));
  check('arming asks to confirm first', !!armDlg, dlg.seen.map(d => d.title));
  check('the confirm warns it will transmit to the inverter', armDlg && /transmit|CAN|listen-only|transmite/i.test(armDlg.message || ''), armDlg && armDlg.message);
  const pre = dlg.seen.find(d => d !== armDlg && /can0/.test(`${d.title} ${d.message}`));
  check('with no CAN port, the arm gate says what is missing (can0), in a dialog', !!pre, dlg.seen.map(d => d.title));
  check('…and the card stays Disarmed', /Disarmed|Dezarmat/.test(await pill(page)), await pill(page));
  const st = await apiVbms(page);
  check('API: nothing armed, nothing running', st['deye-vbms'] && st['deye-vbms'].armed === false
    && st['deye-vbms'].running === false, st['deye-vbms']);
  check('…and still offers Arm', await card.locator('button[data-action="setVbmsArmed"]').count() === 1);
  check('no native confirm/alert was used', dlg.native === 0, dlg.native);
} finally {
  await browser.close();
}

const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
