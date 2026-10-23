/* A schedule in the editor and a force on its card: the entries table,
 * days refused in words, the preview on the clock (never stale), the card's
 * schedule line, editing brings the entries back as the field reads them
 * (Mon-Fri, also on the Romanian page), Force… then ending it, the Romanian
 * page.
 *
 *   docker run -d --rm --name mbg-trim-test -p 127.0.0.1:18090:8080 -e UI_HOST=0.0.0.0 \
 *     --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app multi-bus-gateway:latest main.py
 *   MBG_URL=http://127.0.0.1:18090 CHROMIUM_PATH=<chrome> node rules_schedule_e2e.mjs
 *
 * Config it expects: the one of rules_trim_e2e.mjs (auth off, writes off, a
 * `vfd1` delta_vfd_el with `run_at` enabled, on a host that refuses).
 */
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[39]|422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
const dlg = await watchDialogs(page);
dlg.answer = false;
const answers = dlg.answers;
try {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="rules"]'); await page.waitForTimeout(800);
  const content = page.locator('#rulesContent');
  await page.click('#rulesAddBtn'); await page.waitForTimeout(800);
  const m = page.locator('#ruleModal');
  await m.locator('#rlKind').selectOption('schedule'); await page.waitForTimeout(200);
  check('a schedule shows its entries table with two rows to start from', await m.locator('#rlSched').isVisible()
        && (await m.locator('#rlSched tbody tr').count()) === 2);
  await m.locator('#rlTarget').selectOption('d:vfd1'); await page.waitForTimeout(200);
  await m.locator('#rlCommand').selectOption('run_at');
  await m.locator('#rlId').fill('week-pump');
  await m.locator('#rlLabel').fill('Pump by the week');
  await m.locator('#rlSched tbody tr').nth(0).locator('[data-sc="days"]').fill('luni');
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(800);
  check('days it cannot read are refused in words', /schedule\[0\]\.days/.test(await m.locator('#ruleFeedback').innerText()),
        await m.locator('#ruleFeedback').innerText());
  await m.locator('#rlSched tbody tr').nth(0).locator('[data-sc="days"]').fill('Mon-Fri');
  await m.locator('button:has-text("Add entry")').click();
  const r3 = m.locator('#rlSched tbody tr').nth(2);
  await r3.locator('[data-sc="days"]').fill('Sat,Sun'); await r3.locator('[data-sc="at"]').fill('08:00');
  await r3.locator('[data-sc="value"]').fill('20'); await r3.locator('[data-sc="label"]').fill('weekend');
  await m.locator('#rlDebounce').fill('1'); await m.locator('#rlMinInt').fill('0');
  await m.locator('button:has-text("Preview")').click(); await page.waitForTimeout(1500);
  const pv = await m.locator('#rlPreview').innerText();
  check('the preview names the entry in force and is not stale', /(Mon-Fri|weekend|every day|Sat-Sun|Mon,|normal)/.test(pv) && !/stale/.test(pv), pv);
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(1500);
  const card = content.locator('[data-rule="week-pump"]');
  check('saved in shadow', await card.count() === 1 && /shadow/i.test(await card.innerText()));
  check('the card reads the schedule', /Mon-Fri 06:00 → 25/.test(await card.innerText()) && /weekend: Sat-Sun 08:00 → 20/.test(await card.innerText()),
        (await card.innerText()).slice(0, 400));
  const raw = (await (await page.request.get(BASE + '/api/rules/week-pump')).json());
  const sch = (raw.rule || raw).schedule;
  check('the API keeps days as numbers', JSON.stringify(sch[0].days) === '[1,2,3,4,5]' && JSON.stringify(sch[2].days) === '[6,7]', JSON.stringify(sch));
  // force, then end it
  answers.push({ value: 30, minutes: 60 });
  await card.locator('button:has-text("Force")').click(); await page.waitForTimeout(1500);
  await page.click('#rulesRefreshBtn'); await page.waitForTimeout(1200);
  check('a force shows on the card with its value and end', /forced value 30 until/i.test(await content.locator('[data-rule="week-pump"]').innerText()),
        (await content.locator('[data-rule="week-pump"]').innerText()).slice(0, 500));
  answers.push({ value: '' });
  await content.locator('[data-rule="week-pump"] button:has-text("Force")').click(); await page.waitForTimeout(1500);
  await page.click('#rulesRefreshBtn'); await page.waitForTimeout(1200);
  check('an empty answer ends the force', !/forced value/i.test(await content.locator('[data-rule="week-pump"]').innerText()));
  // the Romanian page: translated days on the card, the field keeps Mon-Fri
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.click('[data-page="rules"]'); await page.waitForTimeout(1200);
  const ro = await content.locator('[data-rule="week-pump"]').innerText();
  await content.locator('[data-rule="week-pump"] button[data-action="openRuleModal"]').click(); await page.waitForTimeout(800);
  check('in Romanian the card says Lu-Vi, the field keeps what the server reads', /Lu-Vi 06:00 → 25/.test(ro)
        && await m.locator('#rlSched tbody tr').nth(0).locator('[data-sc="days"]').inputValue() === 'Mon-Fri'
        && await m.locator('#rlSched tbody tr').nth(2).locator('[data-sc="days"]').inputValue() === 'Sat,Sun'
        && await m.locator('#rlSched tbody tr').nth(2).locator('[data-sc="at"]').inputValue() === '08:00',
        `${ro.slice(0, 300)} | ${await m.locator('#rlSched tbody tr').nth(0).locator('[data-sc="days"]').inputValue()} | ${await m.locator('#rlSched tbody tr').nth(2).locator('[data-sc="days"]').inputValue()}`);
  check('no raw keys', !/rules\.(kindSchedule|sched|force|day)/.test(ro + await m.innerText()));
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  answers.push(true);
  await content.locator('[data-rule="week-pump"] button[data-action="deleteRule"]').click(); await page.waitForTimeout(1500);
  check('deleted', await content.locator('[data-rule="week-pump"]').count() === 0);
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
