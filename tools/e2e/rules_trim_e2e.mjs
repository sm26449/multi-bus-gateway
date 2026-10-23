/* A trim rule in the editor: the kind shows its own fields and hides the
 * others, a wrong band is refused in words, the rule is saved in shadow, its
 * card says the band, the step and the limits, editing brings every field
 * back, and the Romanian page has no raw keys.
 *
 *   docker run -d --rm --name mbg-trim-test -p 127.0.0.1:18090:8080 -e UI_HOST=0.0.0.0 \
 *     --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app multi-bus-gateway:latest main.py
 *   MBG_URL=http://127.0.0.1:18090 CHROMIUM_PATH=<chrome> node rules_trim_e2e.mjs
 *
 * Config it expects: auth off, writes off, a device `vfd1` (template
 * delta_vfd_el) on a host that refuses, with its `run_at` command enabled.
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
  await m.locator('#rlKind').selectOption('trim'); await page.waitForTimeout(200);
  check('trim shows its own fields', await m.locator('[data-rl-kind="trim"]').isVisible()
        && await m.locator('#rlTrimAbove').isVisible() && await m.locator('#rlTrimOff').isVisible());
  check('and hides the steps and the condition', !(await m.locator('[data-rl-kind="steps"]').isVisible())
        && !(await m.locator('[data-rl-kind="condition"]').isVisible()));
  check('every trim field is labelled', (await m.locator('label[for="rlTrimSignal"], label[for="rlTrimAbove"], label[for="rlTrimBelow"], label[for="rlTrimStep"], label[for="rlTrimGain"], label[for="rlTrimMin"], label[for="rlTrimMax"], label[for="rlTrimStart"], label[for="rlTrimOff"]').count()) === 9);
  await m.locator('#rlTarget').selectOption('d:vfd1'); await page.waitForTimeout(200);
  await m.locator('#rlCommand').selectOption('run_at');
  await m.locator('#rlId').fill('vfd-trim');
  await m.locator('#rlLabel').fill('Drive follows the surplus');
  await m.locator('#rlTrimSignal').fill('vfd1.igbt_temperature');
  await m.locator('#rlTrimAbove').fill('40'); await m.locator('#rlTrimBelow').fill('50');   // a wrong band first
  await m.locator('#rlTrimStep').fill('2'); await m.locator('#rlTrimMin').fill('15'); await m.locator('#rlTrimMax').fill('50');
  await m.locator('#rlTrimOff').fill('0');
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(800);
  check('a band upside down is refused in words', /below: must be under above/i.test(await m.locator('#ruleFeedback').innerText()),
        await m.locator('#ruleFeedback').innerText());
  await m.locator('#rlTrimBelow').fill('30');
  await m.locator('button:has-text("Preview")').click(); await page.waitForTimeout(1500);
  check('the preview says there is no signal yet', /no signal|stale/i.test(await m.locator('#rlPreview').innerText()), await m.locator('#rlPreview').innerText());
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(1500);
  const card = content.locator('[data-rule="vfd-trim"]');
  check('saved and listed in shadow', await card.count() === 1 && /shadow/i.test(await card.innerText()));
  const txt = await card.innerText();
  check('the card says the band, the step, the limits and off', /Raise while over 40/.test(txt) && /Lower while under 30/.test(txt)
        && /± 2/.test(txt) && /15…50/.test(txt) && /off 0/.test(txt), txt.slice(0, 300));
  const saved = (await (await page.request.get(BASE + '/api/rules/vfd-trim')).json());
  const r = saved.rule || saved;
  check('the API keeps every trim field', r.kind === 'trim' && r.above === 40 && r.below === 30 && r.step === 2
        && r.min === 15 && r.max === 50 && r.off_value === 0 && r.gain === undefined, JSON.stringify(r).slice(0, 300));
  await card.locator('button[data-action="openRuleModal"]').click(); await page.waitForTimeout(800);
  check('editing brings the kind and the fields back', await m.locator('#rlKind').inputValue() === 'trim'
        && await m.locator('#rlTrimAbove').inputValue() === '40' && await m.locator('#rlTrimOff').inputValue() === '0'
        && await m.locator('#rlTrimGain').inputValue() === '');
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  // the Romanian page
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.click('[data-page="rules"]'); await page.waitForTimeout(800);
  await page.click('#rulesAddBtn'); await page.waitForTimeout(800);
  await m.locator('#rlKind').selectOption('trim'); await page.waitForTimeout(200);
  const ro = await m.innerText();
  check('in Romanian, with no raw keys', /Crește cât timp e peste/.test(ro) && /Valoare de oprire/.test(ro)
        && !/rules\.trim|rules\.kindTrim/.test(ro), ro.slice(0, 200));
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  dlg.answers.push(true);
  await content.locator('[data-rule="vfd-trim"] button[data-action="deleteRule"]').click(); await page.waitForTimeout(1500);
  check('deleted', await content.locator('[data-rule="vfd-trim"]').count() === 0);
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
