/* The Rules page: the empty state, the editor built from parts with a live
 * preview, a new rule saved in shadow, its card in words (state, want, last
 * decision), the arming confirmation that names the target, decisions, and
 * deletion. On the safety fixture the signal cannot be read (hosts refuse),
 * so the honest picture is "stale" — which must be said, never hidden.
 *
 *   MBG_URL=http://localhost:18087 CHROMIUM_PATH=<chrome> node rules_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://localhost:18087';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[39]|422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
try {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  check('Rules is in the main navigation', await page.locator('[data-page="rules"]').count() === 1);
  await page.click('[data-page="rules"]'); await page.waitForTimeout(800);
  const content = page.locator('#rulesContent');
  check('the empty state says what a rule is', /shadow/i.test(await content.innerText()));
  // the editor
  await page.click('#rulesAddBtn'); await page.waitForTimeout(800);
  const m = page.locator('#ruleModal');
  check('the editor opens with a target that offers a command', (await m.locator('#rlTarget option').count()) >= 1
        && (await m.locator('#rlCommand option').allInnerTexts()).includes('power_limit'));
  check('every field is labelled', (await m.locator('label[for="rlId"], label[for="rlSignal"], label[for="rlRelease"], label[for="rlDebounce"]').count()) === 4);
  await m.locator('#rlTarget').selectOption('d:sunfield-u1'); await page.waitForTimeout(200);
  check('picking a unit target offers its commands', (await m.locator('#rlCommand option').allInnerTexts()).includes('power_limit'));
  await m.locator('#rlId').fill('ov-test');
  await m.locator('#rlLabel').fill('OV test');
  await m.locator('#rlSignal').fill('max(sunfield-u1.voltage_l1_n, sunfield-u1.voltage_l2_n)');
  await m.locator('#rlRelease').fill('248');
  await m.locator('#rlParams').fill('revert_s=0, ramp_s=10');
  // the preview: on refusing hosts there is no signal, and it says so
  await m.locator('button:has-text("Preview")').click(); await page.waitForTimeout(1500);
  check('the preview says there is no signal yet', /no signal|stale/i.test(await m.locator('#rlPreview').innerText()), await m.locator('#rlPreview').innerText());
  // a bad release threshold is refused in words
  await m.locator('#rlRelease').fill('260');
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(800);
  check('a wrong release threshold is refused in words', /release_below/i.test(await m.locator('#ruleFeedback').innerText()));
  await m.locator('#rlRelease').fill('248');
  await m.locator('[data-rule-save]').click(); await page.waitForTimeout(1500);
  check('the rule is saved and listed in shadow', await content.locator('[data-rule="ov-test"]').count() === 1
        && /shadow/i.test(await content.locator('[data-rule="ov-test"]').innerText()));
  const card = content.locator('[data-rule="ov-test"]');
  check('the card names the target and the steps', /sunfield-u1 · power_limit/.test(await card.innerText()) && /Warning/.test(await card.innerText()));
  await page.waitForTimeout(6000);   // a few ticks: the signal cannot be read → stale, said in words
  check('with no readable signal the state is stale, in words', /stale/i.test(await card.innerText()), (await card.innerText()).slice(0, 200));
  // arming asks for a confirmation that names the target
  let dialogText = '';
  page.once('dialog', d => { dialogText = d.message(); d.dismiss(); });
  await card.locator('button:has-text("Arm")').click(); await page.waitForTimeout(500);
  check('arming asks a confirmation that names the target', /sunfield-u1 · power_limit/.test(dialogText), dialogText);
  check('dismissed: still shadow', /shadow/i.test(await card.innerText()));
  // accepted — but on this fixture writes are off, so arming is refused in words
  const msgs = [];
  const onDialog = d => { msgs.push(d.message()); d.accept(); };
  page.on('dialog', onDialog);
  await card.locator('button:has-text("Arm")').click(); await page.waitForTimeout(1500);
  page.off('dialog', onDialog);
  check('with writes off, arming is refused in words', msgs.some(x => /writes are disabled/i.test(x)), msgs.join(' | ').slice(0, 160));
  check('still shadow after the refusal', /shadow/i.test(await content.locator('[data-rule="ov-test"]').innerText()));
  // decisions in words
  await content.locator('[data-rule="ov-test"] button:has-text("Decisions")').click(); await page.waitForTimeout(1000);
  const dec = await content.locator('[data-rule-decisions="ov-test"]').innerText();
  check('the decision log says why', /stale|hold|debounce/i.test(dec), dec.slice(0, 120));
  // edit keeps the id disabled
  await content.locator('[data-rule="ov-test"] button[data-action="openRuleModal"]').click(); await page.waitForTimeout(800);
  check('editing keeps the id and the steps', await m.locator('#rlId').isDisabled() && (await m.locator('#rlSteps tbody tr').count()) === 1);
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  // delete
  page.once('dialog', d => d.accept());
  await content.locator('[data-rule="ov-test"] button[data-action="deleteRule"]').click(); await page.waitForTimeout(1500);
  check('deleted: the empty state is back', await content.locator('[data-rule="ov-test"]').count() === 0);
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
