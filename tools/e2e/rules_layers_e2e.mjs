/* Several rules on one target (rules phase 3), in the browser: the Layer
 * field, the layer on the card, a lower rule that says it yields to the
 * higher one, the device's channels over the API, and the Romanian page.
 *
 *   docker run -d --rm --name mbg-trim-test -p 127.0.0.1:18090:8080 -e UI_HOST=0.0.0.0 \
 *     --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app multi-bus-gateway:latest main.py
 *   MBG_URL=http://127.0.0.1:18090 MBG_USER=admin MBG_PASS=<pass> CHROMIUM_PATH=<chrome> node rules_layers_e2e.mjs
 *
 * Config it expects: login on, security.allow_writes true (arming needs
 * both), a `vfd1` delta_vfd_el with `run_at` enabled on a host that REFUSES
 * — a rule's write fails there, nothing reaches a drive.
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(20000);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
await watchDialogs(page);
const api = (path, body, method = 'POST') => ctx.request.fetch(BASE + path, { method, data: body ? JSON.stringify(body) : undefined, headers: { 'Content-Type': 'application/json' } });
const rule = (id, layer, value) => ({ id, mode: 'shadow', layer, target: { device: 'vfd1', command: 'run_at' }, kind: 'condition',
  when: 'between("00:00", "24:00")', then: { params: { value } }, else: null,
  timing: { every_s: 2, debounce: 1, min_interval_s: 0, reassert_s: 0 } });
try {
  const login = await api('/api/auth/login', { username: process.env.MBG_USER || 'admin', password: process.env.MBG_PASS });
  check('logged in', login.ok());
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="rules"]'); await page.waitForTimeout(800);
  const content = page.locator('#rulesContent');
  // the editor: the Layer field, saved and brought back
  await page.click('#rulesAddBtn'); await page.waitForTimeout(800);
  const m = page.locator('#ruleModal');
  check('the Layer field offers the four layers with their priority', (await m.locator('#rlLayer option').allInnerTexts()).join('|') === 'safety (90)|manual (70)|schedule (50)|optimize (30)');
  await m.locator('#rlKind').selectOption('condition');
  await m.locator('#rlTarget').selectOption('d:vfd1'); await page.waitForTimeout(200);
  await m.locator('#rlCommand').selectOption('run_at');
  await m.locator('#rlId').fill('hot-stop'); await m.locator('#rlLabel').fill('Stop when hot');
  await m.locator('#rlLayer').selectOption('safety');
  await m.locator('#rlWhen').fill('between("00:00", "24:00")'); await m.locator('#rlThen').fill('value=0'); await m.locator('#rlElse').fill('');
  await m.locator('#rlDebounce').fill('1'); await m.locator('#rlMinInt').fill('0');
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(1500);
  const hot = content.locator('[data-rule="hot-stop"]');
  check('the card names the layer', /safety/i.test(await hot.innerText()), (await hot.innerText()).slice(0, 200));
  const saved = (await (await api('/api/rules/hot-stop', null, 'GET')).json());
  check('the API keeps the layer', (saved.rule || saved).layer === 'safety');
  // a second rule, lower, on the same channel; both armed
  check('a schedule rule on the same drive is accepted', (await api('/api/rules', rule('day-run', 'schedule', 25))).ok());
  check('the safety rule is armed', (await api('/api/rules/hot-stop/mode', { mode: 'armed' })).ok());
  check('the schedule rule is armed beside it', (await api('/api/rules/day-run/mode', { mode: 'armed' })).ok());
  check('a second rule at the same priority is refused in words', !(await api('/api/rules', { ...rule('day-two', 'schedule', 30), mode: 'armed' })).ok());
  // the safety rule's write fails on the refusing host (retries), so the
  // ticks are slow here: wait for the lower rule's first decision
  let day = '';
  for (let i = 0; i < 15 && !/yields to/i.test(day); i++) {
    await page.waitForTimeout(2000);
    await page.click('#rulesRefreshBtn'); await page.waitForTimeout(800);
    day = await content.locator('[data-rule="day-run"]').innerText();
  }
  check('the lower rule says it yields to the higher one', /yields to hot-stop/i.test(day), day.slice(0, 500));
  const ch = await (await api('/api/channels/vfd1', null, 'GET')).json();
  const motor = (ch.channels || []).find(c => c.channel === 'motor');
  check('the channel view names the winner and the stack', motor && motor.winner === 'hot-stop'
        && motor.rules.map(r => r.rule).join(',') === 'hot-stop,day-run' && motor.settings.min_off_s === 5, JSON.stringify(motor || ch).slice(0, 300));
  await content.locator('[data-rule="hot-stop"] button[data-action="openRuleModal"]').click(); await page.waitForTimeout(800);
  check('editing brings the layer back', await m.locator('#rlLayer').inputValue() === 'safety');
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  // the Romanian page
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.click('[data-page="rules"]'); await page.waitForTimeout(1500);
  const ro = (await content.locator('[data-rule="hot-stop"]').innerText()) + (await content.locator('[data-rule="day-run"]').innerText());
  check('in Romanian: the layer and "yields to"', /siguranță/i.test(ro) && /cedează lui hot-stop/i.test(ro) && !/rules\.(layer|yields)/.test(ro), ro.slice(0, 400));
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  for (const id of ['day-run', 'hot-stop']) await api(`/api/rules/${id}`, null, 'DELETE');
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
