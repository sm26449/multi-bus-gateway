/* F20 (ADR-0041 L2): the restore/import confirm dialog discloses what the bundle
 * would re-enable. app._f20DisclosureText turns the server's would_unlock counts into
 * a plain line (+ the OV-suspension note); the lock select defaults to the safe
 * "lock all writes". (ui/js/app-settings.js)
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node f20_disclosure_e2e.mjs */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const txt = (u, r) => page.evaluate(([u, r]) => app._f20DisclosureText(u, r), [u, r]);

  check('nothing unlocked → reassuring', /Nothing is unlocked/.test(await txt({ nothing: true }, true)));
  const t1 = await txt({ devices: 2, components: 1, units: 0, bindings: 3, rules: 1, nothing: false }, true);
  check('restore lists devices + components + commands + armed rules',
        /2 devices/.test(t1) && /1 component/.test(t1) && /3 enabled commands/.test(t1) && /1 armed rule/.test(t1), t1);
  check('restore includes the OV-suspension note', /SUSPENDS the safety/.test(t1));
  const t2 = await txt({ devices: 2, components: 0, units: 0, bindings: 0, rules: 1, nothing: false }, false);
  check('import never mentions armed rules (no rules.yaml in an import bundle)', !/armed rule/.test(t2) && /2 devices/.test(t2), t2);

  const fld = await page.evaluate(() => app._f20LockField());
  check('the lock option defaults to the safe "lock all writes"', fld.initial === 'lock' && fld.options[0].value === 'lock');

  await ctx.close();
} catch (e) { check('ran', false, String(e).slice(0, 200)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
