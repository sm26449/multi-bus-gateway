/* The Add Installation wizard, driven exactly as an operator would.
 *
 * Adding a device asked three questions and adding a whole installation asked
 * one, which had it backwards. This walks the four steps and then checks the
 * thing that actually matters: that what was created matches what was shown on
 * the review step, down to the device ids and the topic each group publishes on.
 *
 *   MBG_URL=http://localhost:18086 CHROMIUM_PATH=<chrome> node plant_wizard_e2e.mjs
 *
 * Config it expects: auth off, no endpoints yet, pointed anywhere (the wizard
 * never needs the host to answer).
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18086';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
};

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
const errs = [];
page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
page.on('pageerror', e => errs.push(String(e)));

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="devices"]');
  await page.waitForTimeout(1000);

  // ---- the way in --------------------------------------------------------
  const entry = page.locator('button:has-text("Add Installation")');
  check('there is one clear way to add an installation', await entry.count() === 1);
  await entry.click();
  await page.waitForTimeout(700);
  check('the wizard opens', await page.locator('#plantWizardModal').isVisible());
  check('it has four steps', await page.locator('#plantWizSteps .wizard-step').count() === 4);

  // ---- step 1: the installation -----------------------------------------
  check('back is hidden on the first step',
        await page.locator('#plantWizBack').evaluate(e => getComputedStyle(e).visibility) === 'hidden');
  await page.click('#plantWizNext');
  await page.waitForTimeout(400);
  check('a bad id is refused with a reason',
        (await page.locator('#plantWizFeedback').innerText()).length > 10);
  await page.fill('#pwId', 'sunfield');
  await page.fill('#pwName', 'Sunfield PV');
  await page.fill('#pwHost', '127.0.0.1');
  await page.fill('#pwPort', '9');
  await page.click('#plantWizNext');
  await page.waitForTimeout(700);

  // ---- step 2: what it holds --------------------------------------------
  check('step 2 starts with the group nearly every plant has',
        (await page.locator('#plantWizBody').innerText()).includes('inverters'));
  await page.click('#plantWizNext');
  await page.waitForTimeout(400);
  check('a group with no units is refused',
        /unit ID/i.test(await page.locator('#plantWizFeedback').innerText()));
  await page.locator('[data-g="0"][data-f="units"]').fill('1, 2');
  await page.locator('[data-g="0"][data-f="template"]').selectOption('fronius_sunspec_inverter');

  // add the meter group — the whole point of the model
  await page.click('button:has-text("Add group")');
  await page.waitForTimeout(500);
  check('a second group can be added',
        await page.locator('[data-g="1"][data-f="units"]').count() === 1);
  await page.locator('[data-g="1"][data-f="role"]').selectOption('meter');
  await page.waitForTimeout(400);
  check('choosing a role names the group for you',
        await page.locator('[data-g="1"][data-f="id"]').inputValue() === 'grid');
  await page.locator('[data-g="1"][data-f="units"]').fill('240');
  await page.locator('[data-g="1"][data-f="template"]').selectOption('fronius_sunspec_meter');

  // a unit claimed twice must be caught BEFORE anything is created
  await page.locator('[data-g="1"][data-f="units"]').fill('2');
  await page.click('#plantWizNext');
  await page.waitForTimeout(400);
  check('one unit id in two groups is refused',
        /two groups/i.test(await page.locator('#plantWizFeedback').innerText()),
        await page.locator('#plantWizFeedback').innerText());
  await page.locator('[data-g="1"][data-f="units"]').fill('240');
  await page.click('#plantWizNext');
  await page.waitForTimeout(700);

  // ---- step 3: how it is read -------------------------------------------
  const s3 = await page.locator('#plantWizBody').innerText();
  check('step 3 asks how each group is read', /Intervals/i.test(s3));
  check('both ways of reading are offered',
        await page.locator('[data-g="0"][data-f="modbus"]').count() === 1
        && await page.locator('[data-g="0"][data-f="http"]').count() === 1);

  // a group with NO way to be read must be refused
  await page.locator('[data-g="0"][data-f="modbus"]').uncheck();
  await page.waitForTimeout(400);
  await page.click('#plantWizNext');
  await page.waitForTimeout(400);
  check('a group with no way to be read is refused',
        /at least one way/i.test(await page.locator('#plantWizFeedback').innerText()),
        await page.locator('#plantWizFeedback').innerText());

  // Solar API alone is a first-class choice, and the preset fills the exact call
  await page.locator('[data-g="0"][data-f="http"]').check();
  await page.waitForTimeout(400);
  await page.locator('button:has-text("Fronius")').first().click();
  await page.waitForTimeout(400);
  const url = await page.locator('[data-g="0"][data-f="url"]').inputValue();
  check('the preset fills the Solar API call with the unit placeholder',
        url.includes('GetInverterRealtimeData.cgi') && url.includes('${unit_id}'), url);
  check('and picks the matching template',
        await page.locator('[data-g="0"][data-f="httpTemplate"]').inputValue()
          === 'fronius_solar_api_inverter');

  // back on: both sources, fastest first
  await page.locator('[data-g="0"][data-f="modbus"]').check();
  await page.waitForTimeout(400);
  await page.click('#plantWizNext');
  await page.waitForTimeout(700);

  // ---- step 4: review ----------------------------------------------------
  const review = await page.locator('#plantWizBody').innerText();
  check('the review names both groups', /inverters/.test(review) && /grid/.test(review));
  check('it says which devices will be created',
        /sunfield-u1/.test(review) && /sunfield-u2/.test(review) && /sunfield-u240/.test(review),
        review.split('\n').pop());
  check('it shows where each group publishes',
        review.includes('mbg/endpoints/sunfield/') && review.includes('grid'));
  check('the button says what it will do',
        /create/i.test(await page.locator('#plantWizNextLabel').innerText()));

  // ---- create, then verify against the review ---------------------------
  await page.click('#plantWizNext');
  await page.waitForTimeout(3500);
  const d = await (await fetch(`${BASE}/api/endpoints/sunfield`)).json();
  check('the installation exists', d.id === 'sunfield', JSON.stringify(d).slice(0, 90));
  const g = Object.fromEntries((d.groups || []).map(x => [x.id, x]));
  check('both groups were created', !!g.inverters && !!g.grid);
  check('the inverters carry both sources, fastest first',
        (g.inverters.sources || []).map(s => s.id).join(',') === 'solar_api,modbus',
        (g.inverters.sources || []).map(s => s.id).join(','));
  check('the meter group is its own', g.grid.total_units === 1 && g.grid.role === 'meter');
  check('the first group owns the headline topic, a group of one has none',
        g.inverters.topic === 'mbg/endpoints/sunfield'
        && g.grid.topic === null);
  check('the wizard closed on success',
        !(await page.locator('#plantWizardModal').isVisible()));

  check('no console errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) {
  check('script completed', false, e.message);
} finally {
  const ok = results.filter(r => r.ok).length;
  console.log(`\n${ok}/${results.length} checks passed`);
  await browser.close();
  process.exit(ok === results.length ? 0 : 1);
}
