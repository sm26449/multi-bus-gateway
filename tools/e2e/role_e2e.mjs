/* The gateway's role, as an operator sees it: an `ha` service token can be
 * made from Settings → Security; a standby gateway says so on every page
 * (since when, the epoch, who set it, why) and refuses a command; becoming
 * active again clears it; a module from `modules:` is listed in Settings →
 * Modules (the top menu has no room), with whether it answers.
 *
 *   docker run -d --rm --name mbg-role-test -p 127.0.0.1:18090:8080 -e UI_HOST=0.0.0.0 \
 *     --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app <image> main.py
 *   MBG_URL=http://127.0.0.1:18090 MBG_USER=admin MBG_PASS=<pass> CHROMIUM_PATH=<chrome> node role_e2e.mjs
 *
 * Config it expects: login on, a device `vfd1` with the command run_at
 * enabled, writes allowed, and modules: [{id: ha, name: Redundancy, icon: shield-check, url: /ha/, health: /health}].
 */
import { chromium, request as pwRequest } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
const page = await ctx.newPage();
page.setDefaultTimeout(20000);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422|503/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
try {
  const login = await ctx.request.post(BASE + '/api/auth/login', { data: { username: process.env.MBG_USER || 'admin', password: process.env.MBG_PASS } });
  check('logged in', login.ok());
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  check('the top menu stays as it was', await page.locator('nav [data-module]').count() === 0);
  check('an active gateway shows no banner', await page.locator('#roleBanner').isHidden());

  // an ha token, made from Settings → Security like any other
  await page.click('[data-page="config"]'); await page.waitForTimeout(800);
  await page.click('[data-cfgtab="modules"]'); await page.waitForTimeout(500);
  const row = page.locator('[data-cfgpanel="modules"] .module-row[data-module="ha"]');
  check('a module is in Settings → Modules, with Open', await row.count() === 1
        && /Redundancy/.test(await row.innerText()) && await row.locator('a').getAttribute('href') === '/ha/');
  check('…said not to be part of the open-source gateway', /not part of the open-source/.test(await page.locator('[data-cfgpanel="modules"]').innerText()));
  await page.waitForTimeout(800);
  check('…with whether it answers', /answers/i.test(await row.locator('.module-health').innerText()), await row.locator('.module-health').innerText());
  check('the Devices page no longer carries the ESP link', await page.locator('#nodesLink').count() === 0);
  await page.click('[data-cfgtab="security"]'); await page.waitForTimeout(800);
  check('the ha role is offered for a service token', await page.locator('#tokRole option[value="ha"]').count() === 1);
  const made = await ctx.request.post(BASE + '/api/auth/service-tokens', { data: { name: 'mbg-ha', role: 'ha' } });
  const tok = (await made.json()).token;
  const ha = { Authorization: `Bearer ${tok}` };
  // the service holds no session: its own context, the token alone
  const svc = await pwRequest.newContext();

  const set = await svc.put(BASE + '/api/role', { headers: ha, data: { role: 'standby', epoch: 1, reason: 'partner A is active' } });
  check('the ha service makes the gateway standby', set.ok());
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.waitForTimeout(800);
  const banner = await page.locator('#roleBanner').innerText().catch(() => '');
  check('every page says it is standby, since when, the epoch and why', /Standby/.test(banner) && /epoch 1/.test(banner)
        && /partner A is active/.test(banner) && /token:mbg-ha/.test(banner), banner);
  const cmd = await ctx.request.post(BASE + '/api/devices/vfd1/commands/run_at', { data: { params: { hz: 20 } } });
  check('a command is refused, in words', cmd.status() === 503 && /standby/.test(await cmd.text()));
  const lb = await ctx.request.get(BASE + '/api/role');
  check('a load balancer reads 503 from a standby', lb.status() === 503);

  await svc.put(BASE + '/api/role', { headers: ha, data: { role: 'active', epoch: 2, reason: 'test over' } });
  await page.reload(); await page.waitForLoadState('networkidle'); await page.waitForTimeout(800);
  check('active again: the banner is gone', await page.locator('#roleBanner').isHidden());
  const old = await svc.put(BASE + '/api/role', { headers: ha, data: { role: 'standby', epoch: 1 } });
  check('an older epoch is refused', old.status() === 409);

  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await svc.put(BASE + '/api/role', { headers: ha, data: { role: 'standby', epoch: 3 } });
  await page.reload(); await page.waitForLoadState('networkidle'); await page.waitForTimeout(800);
  check('in Romanian', /acest gateway e rezerva/.test(await page.locator('#roleBanner').innerText()));
  await svc.put(BASE + '/api/role', { headers: ha, data: { role: 'active', epoch: 4 } });
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  await ctx.request.delete(BASE + '/api/auth/service-tokens/mbg-ha');
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
