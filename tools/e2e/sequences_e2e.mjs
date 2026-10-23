/* Sequences on the Rules page: written as YAML (a missing device refused in
 * words), the card with its steps in words, a dry run that writes nothing, a
 * run whose command fails (the drive's host refuses) and is undone, a run
 * cancelled in its wait, and the Romanian page.
 *
 *   docker run -d --rm --name mbg-trim-test -p 127.0.0.1:18090:8080 -e UI_HOST=0.0.0.0 \
 *     --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app multi-bus-gateway:latest main.py
 *   MBG_URL=http://127.0.0.1:18090 MBG_USER=admin MBG_PASS=<pass> CHROMIUM_PATH=<chrome> node sequences_e2e.mjs
 *
 * Config it expects: the one of rules_layers_e2e.mjs (login on, writes on, a
 * `vfd1` delta_vfd_el with `run_at` enabled on a host that REFUSES).
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
const dlg = await watchDialogs(page);
const answers = dlg.answers;
const api = (path, body, method = 'POST') => ctx.request.fetch(BASE + path, { method, data: body ? JSON.stringify(body) : undefined, headers: { 'Content-Type': 'application/json' } });
const card = id => page.locator(`[data-sequence="${id}"]`);
async function until(fn, ms = 20000) { const end = Date.now() + ms; while (Date.now() < end) { if (await fn()) return true; await page.waitForTimeout(500); } return false; }
const KICK = `id: kick
label: Kick the pump
params:
  speed: { min: 15, max: 50, default: 20, unit: Hz }
steps:
  - command: { device: vfd1, name: run_at, params: { value: '\${speed}' } }
  - wait_s: 1
  - check: 'hour() >= 0'
on_abort:
  - command: { device: vfd1, name: run_at, params: { value: 0 } }
`;
try {
  check('logged in', (await api('/api/auth/login', { username: process.env.MBG_USER || 'admin', password: process.env.MBG_PASS })).ok());
  for (const id of ['kick', 'soak']) {                  // a previous run's leftovers
    await api(`/api/sequences/${id}/cancel`, {});
    for (let i = 0; i < 40 && !(await api(`/api/sequences/${id}`, null, 'DELETE')).ok() && (await api(`/api/sequences/${id}`, null, 'GET')).ok(); i++)
      await new Promise(r => setTimeout(r, 500));
  }
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="rules"]'); await page.waitForTimeout(1000);
  check('the Rules page has a Sequences section that says what one is', /undone in reverse/i.test(await page.locator('#rulesPage').innerText())
        && /No sequences yet/i.test(await page.locator('#sequencesContent').innerText()));
  // write one: the example refers to a valve this installation does not have
  await page.click('#seqAddBtn'); await page.waitForTimeout(600);
  const m = page.locator('#seqModal');
  check('the editor starts from an example', /wait_until/.test(await m.locator('#seqText').inputValue()));
  await m.locator('button:has-text("Save")').click(); await page.waitForTimeout(800);
  check('a missing device is refused in words', /no device 'valve1'/.test(await m.locator('#seqFeedback').innerText()), await m.locator('#seqFeedback').innerText());
  await m.locator('#seqText').fill(KICK);
  await m.locator('button:has-text("Save")').click(); await page.waitForTimeout(1200);
  check('saved: the card lists the steps in words', await card('kick').count() === 1
        && /vfd1 · run_at \(value=\$\{speed\}\)/.test(await card('kick').innerText()) && /wait 1 s/.test(await card('kick').innerText())
        && /If it fails or is cancelled: vfd1 · run_at \(value=0\)/.test(await card('kick').innerText()), (await card('kick').innerText()).slice(0, 300));
  // dry run: the plan, nothing written
  // (the drive's host refuses: the dry run cannot read what the command
  // checks first, and says so — nothing is written either way)
  answers.push(25);
  await card('kick').locator('button[data-action="dryRunSequence"]').click();
  await until(async () => await card('kick').locator('[data-seq-plan]').count() === 1);
  check('a dry run shows its plan and why it stopped', /Dry run: .*✗ vfd1 run_at \{'value': 25\} · error · the device did not answer the pre-write read — nothing written/s.test(await card('kick').innerText()),
        (await card('kick').innerText()).slice(0, 400));
  // a real run: the refusing host fails the command, the undo is tried, and it says why
  answers.push(20);                // one dialog: the parameter and the write
  await card('kick').locator('button[data-action="runSequence"]').click();
  const ended = await until(async () => /Last run: aborted/i.test(await card('kick').innerText()), 40000);
  const txt = await card('kick').innerText();
  check('a run whose command fails is abandoned and says why', ended && /step 1/.test(txt), txt.slice(0, 400));
  const v = await (await api('/api/sequences/kick', null, 'GET')).json();
  check('the undo was tried after the failure', (v.last[0].steps || []).some(s => s.what === 'on_abort'), JSON.stringify(v.last[0].steps).slice(0, 300));
  // a run cancelled while it waits
  check('a waiting sequence is saved', (await api('/api/sequences', { id: 'soak', label: 'Soak', steps: [{ wait_s: 60 }, { command: { device: 'vfd1', name: 'run_at', params: { value: 15 } } }] })).ok());
  await page.click('#rulesRefreshBtn'); await page.waitForTimeout(1200);
  answers.push(true);
  await card('soak').locator('button[data-action="runSequence"]').click();
  check('while it runs, the card offers Cancel', await until(async () => await card('soak').locator('button[data-action="cancelSequence"]').count() === 1, 10000));
  await card('soak').locator('button[data-action="cancelSequence"]').click();
  check('cancelled, in words', await until(async () => /cancelled/i.test(await card('soak').innerText()), 15000), (await card('soak').innerText()).slice(0, 300));
  // Romanian
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.click('[data-page="rules"]'); await page.waitForTimeout(1500);
  const ro = await page.locator('#rulesPage').innerText();
  check('in Romanian', /Secvențe/.test(ro) && /Rulează…/.test(ro) && /Simulare/.test(ro) && /abandonată|oprită/i.test(ro) && !/seq\./.test(ro), ro.slice(-600));
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  for (const id of ['kick', 'soak']) await api(`/api/sequences/${id}`, null, 'DELETE');
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
