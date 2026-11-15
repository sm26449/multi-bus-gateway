/* UX-14: one shared non-data state component. app._stateBlock renders a consistent
 * loading/empty/no-sample/no-permission/error block with an honest title + reason,
 * an optional single action, and role=status for async announcement without stealing
 * focus (ui/js/app-core.js). Adopted by the fleet filtered-empty state.
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node state_block_e2e.mjs
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
  await page.waitForSelector('#fleetView', { timeout: 20000 });

  const sb = o => page.evaluate(opt => app._stateBlock(opt), o);

  check('loading kind uses the hourglass icon', /state-loading/.test(await sb({ kind: 'loading' })) && /bi-hourglass-split/.test(await sb({ kind: 'loading' })));
  check('no-permission kind uses the lock icon', /bi-lock/.test(await sb({ kind: 'nopermission' })));
  const err = await sb({ kind: 'error', title: 'Could not load', reason: 'the server returned 500', live: true });
  check('error block carries title + reason', /Could not load/.test(err) && /the server returned 500/.test(err));
  check('a live block announces via role=status aria-live', /role="status"/.test(err) && /aria-live="polite"/.test(err));
  check('a non-live block has no role=status', !/role="status"/.test(await sb({ kind: 'empty' })));
  check('an action is included when supplied', /data-x=1/.test(await sb({ kind: 'empty', actionHtml: '<button data-x=1>Retry</button>' })));

  // adoption: the fleet filtered-empty state uses the shared block with a hint
  const adopted = await page.evaluate(() => {
    app.fleetData = { devices: [{ id: 'ok1', name: 'OK One', health: 'ok', enabled: true, protocol: 'tcp', hero: [] }], endpoints: [] };
    app._fleetQuery = 'zzz-nothing-matches'; app._fleetProblems = false;
    app.renderFleet();
    const blk = document.querySelector('#fleetView .state-block');
    const r = { has: !!blk, live: blk && blk.getAttribute('role') === 'status', hint: blk ? /Clear the search/.test(blk.textContent) : false };
    app._fleetQuery = ''; app.renderFleet();
    return r;
  });
  check('the fleet filtered-empty state uses the shared block', adopted.has);
  check('…announced via role=status with a helpful reason', adopted.live && adopted.hint, adopted);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
