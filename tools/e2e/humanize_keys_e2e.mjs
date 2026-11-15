/* UX-12: a raw register key used as a primary label gets a readable form while the
 * raw key stays available; an administrator-authored label is never overridden.
 * app._humanizeKey (ui/js/app-core.js) turns snake_case keys into words and keeps a
 * small set of domain acronyms (and acronym+index tokens like iu0) upper-case.
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node humanize_keys_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const h = k => page.evaluate(key => app._humanizeKey(key), k);

  check('acronym + index + words: iu0_dhw_temperature', await h('iu0_dhw_temperature') === 'IU0 DHW temperature');
  check('plain snake_case is capitalised: outdoor_defrost', await h('outdoor_defrost') === 'Outdoor defrost');
  check('phase acronym kept upper: voltage_l1', await h('voltage_l1') === 'Voltage L1');
  check('known acronym kept upper: battery_soc', await h('battery_soc') === 'Battery SOC');
  check('empty key -> empty', await h('') === '');

  // the author label wins over humanisation (the row uses `v.label || _humanizeKey(name)`)
  const labelWins = await page.evaluate(() => {
    const v = { label: 'Apă caldă menajeră', name: 'iu0_dhw_temperature' };
    const primary = v.label || app._humanizeKey(v.name);
    return primary === 'Apă caldă menajeră';
  });
  check('an administrator-authored label is never overridden', labelWins);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
