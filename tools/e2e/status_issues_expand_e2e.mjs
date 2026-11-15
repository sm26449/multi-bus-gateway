/* UX-09: the Status "Needs attention" list capped at six with a dead "and N more"
 * line that just pointed at the pills. It now exposes an explicit "Show all N
 * issues" button that reveals every issue in place, and a "Show fewer" to collapse
 * (ui/js/app-status.js _stAttention + toggleStatusIssues).
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node status_issues_expand_e2e.mjs
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
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});

  const render = expanded => page.evaluate(exp => {
    app._st = app._st || { data: {}, at: {}, memo: {} };
    app._st.attnExpanded = exp;
    const issues = Array.from({ length: 8 }, (_, k) => ({ key: 'k' + k, sev: k === 0 ? 'error' : 'warn', name: 'Issue ' + (k + 1) }));
    const html = app._stAttention({ issues });
    return { rows: (html.match(/class="st-issue /g) || []).length, html };
  }, expanded);

  // collapsed: 6 rows + an explicit "Show all 8 issues" button
  let r = await render(false);
  check('collapsed shows the capped 6 rows', r.rows === 6, r.rows);
  check('collapsed offers an explicit "Show all 8 issues" action',
        /toggleStatusIssues/.test(r.html) && /Show all 8 issues/.test(r.html) && /aria-expanded="false"/.test(r.html));
  check('the dead "and N more" text is gone', !/the pills and tabs marked/.test(r.html));

  // expanded: all 8 rows + "Show fewer"
  r = await render(true);
  check('expanded reveals all 8 issues', r.rows === 8, r.rows);
  check('expanded offers "Show fewer" and marks aria-expanded', /Show fewer/.test(r.html) && /aria-expanded="true"/.test(r.html));

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
