/* The operator's hand on commands: the Commands… button on a group whose
 * template offers them, the dialog built from the command's parameters, Test
 * (dry run) and Run with verdicts in words, the Commands tab on the unit page.
 * Against the safety fixture (hosts that refuse, writes off) the honest
 * outcome is a refusal — which is exactly what must be shown, never a silent
 * nothing.
 *
 *   MBG_URL=http://localhost:18087 CHROMIUM_PATH=<chrome> node commands_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://localhost:18087';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
// a 403 from a command is the expected answer on this fixture (writes off), not a page error
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/403|502/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
try {
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]'); await page.waitForTimeout(1000);
  await page.locator('.endpoint-row button[data-action="openEndpointDetail"]').first().click(); await page.waitForTimeout(2000);
  const inv = page.locator('[data-group="inverters"]');
  check('the inverter card offers Commands…', await inv.locator('button[data-action="openCommandModal"]').count() === 1);
  check('the meter card does not (its template declares none)', await page.locator('[data-group="grid"] button[data-action="openCommandModal"]').count() === 0);
  check('the unit table has a Limit column', /LIMIT/i.test(await inv.locator('thead').innerText()));
  await inv.locator('button[data-action="openCommandModal"]').click(); await page.waitForTimeout(500);
  const m = page.locator('#endpointModal');
  check('the dialog offers the two presets', (await m.locator('#cmdName option').allInnerTexts()).join('|').match(/limit/i) && await m.locator('#cmdName option').count() === 2);
  check('the form is built from the parameters, all labelled',
        (await m.locator('label[for="cmdP_value"], label[for="cmdP_revert_s"], label[for="cmdP_ramp_s"], label[for="cmdScope"]').count()) === 4);
  check('revert defaults to 600 s', (await m.locator('#cmdP_revert_s').inputValue()) === '600');
  check('the value field carries its bounds', (await m.locator('#cmdP_value').getAttribute('max')) === '100');
  check('the scope offers every unit of the group', await m.locator('#cmdScope option').count() === 3);
  check('the MQTT topic is said', /\/cmd\/power_limit/.test(await m.locator('#cmdTopic').innerText()));
  // a value out of bounds never leaves the browser
  await m.locator('#cmdP_value').fill('150');
  await m.locator('button:has-text("Test")').click(); await page.waitForTimeout(300);
  check('out of bounds is refused before any request', /maximum/i.test(await m.locator('#endpointFeedback').innerText()));
  // Test (dry run) on refusing hosts: an error in words, per unit
  await m.locator('#cmdP_value').fill('60');
  await m.locator('button:has-text("Test")').click();
  await page.waitForFunction(() => !/Loading/.test(document.getElementById('cmdOut')?.innerText || 'Loading'), null, { timeout: 40000 });
  const dry = await m.locator('#cmdOut').innerText();
  check('a dry run answers in words for each unit', (dry.match(/failed|refused|would write/gi) || []).length >= 2, dry.slice(0, 120));
  // Run with writes off: the gate is said
  page.once('dialog', d => d.accept());
  await m.locator('[data-endpoint-save]').click(); await page.waitForTimeout(3000);
  const fb = (await m.locator('#endpointFeedback').innerText()) + ' ' + (await m.locator('#cmdOut').innerText());
  check('with writes off the refusal is said in words', /disabled|refused|failed|write/i.test(fb), fb.slice(0, 120));
  // the restore alias has no parameters
  await m.locator('#cmdName').selectOption('restore'); await page.waitForTimeout(200);
  check('restore takes no parameters and says so', await m.locator('#cmdParams input').count() === 0);
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  // the group editor lets the operator tick commands
  await inv.locator('button[data-action="openGroupModal"]').click(); await page.waitForTimeout(500);
  check('the group editor lists the commands it accepts', await m.locator('[data-grp-cmd]').count() === 2
        && await m.locator('[data-grp-cmd="power_limit"]').isChecked());
  await m.locator('.modal-close').click(); await page.waitForTimeout(300);
  // the unit page has a Commands tab
  await page.locator(`[data-group-units="inverters"] tr button[data-action="openDeviceDetail"]`).first().click(); await page.waitForTimeout(2000);
  const tab = page.locator('#deviceWsTabs [data-dtab="commands"]');
  check('the unit page has a Commands tab', await tab.count() === 1);
  await tab.click(); await page.waitForTimeout(1500);
  const panel = page.locator('#deviceDetailView [data-dpanel="commands"]');
  check('it lists both commands with Run and Test', await panel.locator('.cmd-card').count() === 2
        && await panel.locator('.cmd-card[data-cmd="power_limit"] button:has-text("Run")').count() === 1);
  check('the history section is there', /Recent commands/i.test(await panel.innerText()));
  // a required field left empty is refused in words, before any request
  await panel.locator('.cmd-card[data-cmd="power_limit"] button:has-text("Test")').click(); await page.waitForTimeout(300);
  check('an empty required field is refused in words', /required/i.test(await panel.locator('.cmd-card[data-cmd="power_limit"] .cmd-out').innerText()));
  await panel.locator('#dcmd_power_limit_value').fill('60');
  await panel.locator('.cmd-card[data-cmd="power_limit"] button:has-text("Test")').click();
  await page.waitForFunction(() => !/Loading/.test(document.querySelector('#deviceDetailView .cmd-card[data-cmd="power_limit"] .cmd-out')?.innerText || 'Loading'), null, { timeout: 40000 });
  check('a unit dry run answers in words', /failed|refused|would write/i.test(await panel.locator('.cmd-card[data-cmd="power_limit"] .cmd-out').innerText()));
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
