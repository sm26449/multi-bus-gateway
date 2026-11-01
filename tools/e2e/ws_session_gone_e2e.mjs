/* A UI tab whose WebSocket handshake is refused must not re-dial every 3 s
 * forever (WS-LOG-1: 1,295 refused handshakes an hour in the gateway log).
 *
 * 1. The session dies behind the page's back (a logout from elsewhere with
 *    the same cookie), then the gateway restarts so the live socket drops: the
 *    page must ask /api/auth/status, see the session gone, show the login
 *    screen and never dial /ws again.
 * 2. Same, but /api/auth/status is made to say the session lives (what a
 *    refusal for another reason looks like from the page — an IP allowlist, a
 *    proxy that rewrites Host): the page keeps trying, backing off towards one
 *    dial a minute instead of one every 3 s.
 *
 *   MBG_FX_NAME=mbg-fx-wslog tools/e2e/fixture_up.sh drive 18190 --login <pass>
 *   MBG_URL=http://127.0.0.1:18190 MBG_PASS=<pass> MBG_CONTAINER=mbg-fx-wslog \
 *     CHROMIUM_PATH=<chrome> node tools/e2e/ws_session_gone_e2e.mjs
 *   docker rm -f mbg-fx-wslog
 */
import { chromium, request as pwRequest } from 'playwright';
import { execFileSync } from 'node:child_process';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18190';
const CONTAINER = process.env.MBG_CONTAINER || 'mbg-fx-wslog';
const LOGIN = '#loginOverlay';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const up = async () => {
  for (let i = 0; i < 40; i++) {
    try { if ((await fetch(BASE + '/api/auth/status')).ok) return true; } catch (e) {}
    await sleep(1000);
  }
  return false;
};

// A signed-in page whose session is then ended from elsewhere and whose
// socket drops with a gateway restart. Returns the page and its dial log.
async function sessionEndsUnder(browser, { statusSaysAlive }) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const dials = [], asks = [];
  page.on('websocket', ws => { if (ws.url().endsWith('/ws')) dials.push(Date.now()); });
  page.on('request', rq => { if (rq.url().endsWith('/api/auth/status')) asks.push(Date.now()); });
  const login = await ctx.request.post(BASE + '/api/auth/login',
    { data: { username: process.env.MBG_USER || 'admin', password: process.env.MBG_PASS } });
  check('logged in', login.ok());
  await page.goto(BASE + '/');
  await page.waitForTimeout(3000);
  check('the page opened its websocket', dials.length >= 1, dials.length);
  if (statusSaysAlive) {
    await page.route('**/api/auth/status', r => r.fulfill({
      contentType: 'application/json', body: JSON.stringify({ enabled: true, role: 'admin' }) }));
  }
  // the same cookie logs out from elsewhere: the page still holds it
  const cookie = (await ctx.cookies()).map(c => `${c.name}=${c.value}`).join('; ');
  const other = await pwRequest.newContext({ extraHTTPHeaders: { Cookie: cookie } });
  check('the session is ended from elsewhere', (await other.post(BASE + '/api/auth/logout')).ok());
  await other.dispose();
  // the gateway restarts: the live socket drops, every re-dial is refused
  execFileSync('docker', ['restart', CONTAINER], { stdio: 'ignore' });
  check('the gateway is back', await up());
  return { ctx, page, dials, asks, from: { dials: dials.length, asks: asks.length } };
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
try {
  {
    const s = await sessionEndsUnder(browser, { statusSaysAlive: false });
    await s.page.waitForTimeout(20000);
    const redials = s.dials.length - s.from.dials;
    check('1. the page asked whether its session lives', s.asks.length > s.from.asks, s.asks.length - s.from.asks);
    check('1. …showed the login screen', await s.page.locator(LOGIN).isVisible().catch(() => false));
    check('1. …and stopped re-dialling the websocket (≤ 2 dials in 20 s)', redials <= 2, redials);
    await s.ctx.close();
  }
  {
    const s = await sessionEndsUnder(browser, { statusSaysAlive: true });
    await s.page.waitForTimeout(45000);
    const redials = s.dials.length - s.from.dials;
    // a fixed 3 s retry dials ~15 times in 45 s; 3, 6, 12, 24 s… dials ~5
    check('2. a refusal with a live session backs off (≤ 6 dials in 45 s, not ~15)', redials <= 6, redials);
    const gaps = s.dials.slice(s.from.dials).map((t, i, a) => i ? t - a[i - 1] : 0).slice(1);
    check('2. …each wait longer than the last', gaps.every((g, i) => !i || g > gaps[i - 1]), gaps);
    check('2. …and is not logged out for it', !(await s.page.locator(LOGIN).isVisible().catch(() => false)));
    await s.ctx.close();
  }
} catch (e) {
  check('no exception', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
