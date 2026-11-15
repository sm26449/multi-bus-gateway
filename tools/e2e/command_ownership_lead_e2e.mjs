/* UX-08: the commands tab leads with a compact, honest ownership sentence —
 * "Controlled by <rule(s)> · manual control available/blocked" (or "Manual
 * control — no rule drives this unit") — derived from the channel data, before
 * the full "Who drives it" table (ui/js/app-devices.js _cmdOwnershipLead).
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node command_ownership_lead_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const lead = chans => page.evaluate(c => app._cmdOwnershipLead(c), chans);

  check('no channels -> no lead', await lead([]) === '');

  const manual = await lead([{ channel: 'c1', winner: null, owner: null, rules: [] }]);
  check('no rule -> "Manual control — no rule drives this unit"',
        /Manual control/.test(manual) && /no rule drives/.test(manual), manual);

  const owned = await lead([{ channel: 'c1', winner: 'ov-u1', rules: [{ rule: 'ov-u1' }] }]);
  check('a winning rule -> "Controlled by ov-u1 · manual control available"',
        /Controlled by ov-u1/.test(owned) && /available/.test(owned) && !/blocked/.test(owned), owned);

  const held = await lead([{ channel: 'c1', safety_holder: 'safety-1', rules: [{ rule: 'safety-1' }] }]);
  check('a safety holder -> "Controlled by safety-1 · manual control blocked"',
        /Controlled by safety-1/.test(held) && /blocked/.test(held) && /safety-1/.test(held), held);

  // leads from several channels are de-duplicated and joined
  const multi = await lead([{ channel: 'a', winner: 'r1' }, { channel: 'b', winner: 'r2' }, { channel: 'c', winner: 'r1' }]);
  check('multiple channels -> distinct leads joined', /Controlled by r1, r2/.test(multi), multi);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
