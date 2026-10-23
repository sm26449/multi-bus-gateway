/* Rules with time, in the editor: a condition on the clock alone (never
 * stale), the timers folded under their own heading, saved and brought back,
 * the card's Today column counting what shadow would have started, and the
 * Romanian page.
 *
 *   docker run -d --rm --name mbg-trim-test -p 127.0.0.1:18090:8080 -e UI_HOST=0.0.0.0 \
 *     --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app multi-bus-gateway:latest main.py
 *   MBG_URL=http://127.0.0.1:18090 CHROMIUM_PATH=<chrome> node rules_timers_e2e.mjs
 *
 * Config it expects: the one of rules_trim_e2e.mjs (auth off, writes off, a
 * `vfd1` delta_vfd_el with `run_at` enabled, on a host that refuses).
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const dlg = await watchDialogs(page);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[39]|422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
try {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="rules"]'); await page.waitForTimeout(800);
  const content = page.locator('#rulesContent');
  await page.click('#rulesAddBtn'); await page.waitForTimeout(800);
  const m = page.locator('#ruleModal');
  await m.locator('#rlKind').selectOption('condition'); await page.waitForTimeout(200);
  check('the time functions are named where the condition is written', /between\("10:00", "16:00"\)/.test(await m.locator('[data-rl-kind="condition"]').innerText()));
  check('the timers are folded away on a new rule', !(await m.locator('details:has(#rlMinOn)').getAttribute('open') !== null));
  await m.locator('details:has(#rlMinOn) summary').click(); await page.waitForTimeout(200);
  check('every timer is labelled', (await m.locator('label[for="rlForS"], label[for="rlMinOn"], label[for="rlMinOff"], label[for="rlMaxOn"], label[for="rlMaxStarts"], label[for="rlOff"]').count()) === 6);
  await m.locator('#rlTarget').selectOption('d:vfd1'); await page.waitForTimeout(200);
  await m.locator('#rlCommand').selectOption('run_at');
  await m.locator('#rlId').fill('day-pump');
  await m.locator('#rlLabel').fill('Pump in the day');
  await m.locator('#rlWhen').fill('between("00:00", "24:00")');
  await m.locator('#rlThen').fill('value=20');
  await m.locator('#rlElse').fill('value=0');
  await m.locator('#rlDebounce').fill('1');
  await m.locator('#rlMinInt').fill('0');
  await m.locator('#rlMaxOn').fill('600');                                  // without a rest: refused in words
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(800);
  check('a maximum run without a rest is refused in words', /min_off_s/.test(await m.locator('#ruleFeedback').innerText()),
        await m.locator('#ruleFeedback').innerText());
  await m.locator('#rlMinOff').fill('120'); await m.locator('#rlMinOn').fill('300'); await m.locator('#rlMaxStarts').fill('4');
  await m.locator('button:has-text("Preview")').click(); await page.waitForTimeout(1500);
  check('the preview of a clock rule is not stale', /→ true/.test(await m.locator('#rlPreview').innerText()) && !/stale/.test(await m.locator('#rlPreview').innerText()),
        await m.locator('#rlPreview').innerText());
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(1500);
  const card = content.locator('[data-rule="day-pump"]');
  check('saved in shadow', await card.count() === 1 && /shadow/i.test(await card.innerText()));
  await page.waitForTimeout(5000); await page.click('#rulesRefreshBtn'); await page.waitForTimeout(1200);
  const txt = await content.locator('[data-rule="day-pump"]').innerText();
  check('it decides on the clock alone: true, not stale', /State: true/.test(txt) && !/stale/.test(txt), txt.slice(0, 260));
  check('the card counts the start shadow would have made, today', /Today/i.test(txt) && /0 min · 1×/.test(txt), txt.slice(0, 400));
  const r = (await (await page.request.get(BASE + '/api/rules/day-pump')).json());
  const raw = r.rule || r;
  check('the API keeps the timers and the off value', raw.timing.min_on_s === 300 && raw.timing.min_off_s === 120
        && raw.timing.max_on_s === 600 && raw.timing.max_starts_per_h === 4 && raw.timing.for_s === undefined,
        JSON.stringify(raw.timing));
  await content.locator('[data-rule="day-pump"] button[data-action="openRuleModal"]').click(); await page.waitForTimeout(800);
  check('editing opens the timers with their values', await m.locator('details:has(#rlMinOn)').getAttribute('open') !== null
        && await m.locator('#rlMinOn').inputValue() === '300' && await m.locator('#rlMaxStarts').inputValue() === '4');
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.click('[data-page="rules"]'); await page.waitForTimeout(1200);
  const ro = await content.locator('[data-rule="day-pump"]').innerText();
  await content.locator('[data-rule="day-pump"] button[data-action="openRuleModal"]').click(); await page.waitForTimeout(800);
  const rom = await m.innerText();
  check('in Romanian: the Today column and the timers', /Azi/i.test(ro) && /Funcționare minimă/.test(rom) && !/rules\.(timers|minOn|today)/.test(rom + ro));
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  dlg.answers.push(true);
  await content.locator('[data-rule="day-pump"] button[data-action="deleteRule"]').click(); await page.waitForTimeout(1500);
  check('deleted', await content.locator('[data-rule="day-pump"]').count() === 0);
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
