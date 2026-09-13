/* The Add Installation wizard, driven exactly as an operator would.
 *
 * It used to force a Modbus host on a Solar-API-only installation, offer all
 * fifteen templates to an inverter group, ask for unit ids the datalogger
 * knows, and take intervals as `normal=20, slow=120`. This walks the four steps
 * against a stand-in datalogger and then checks that what was created matches
 * what the review showed — device ids, sources, intervals and topics.
 *
 *   FAKE_PORT=18099 node fake_solar_api.mjs &
 *   MBG_URL=http://localhost:18086 FAKE=127.0.0.1:18099 CHROMIUM_PATH=<chrome> \
 *     node plant_wizard_e2e.mjs
 *
 * Config it expects: auth off, no endpoints yet, and
 * `security.allow_nonlan_http_devices: true` so the stand-in on loopback can
 * be discovered.
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:18086';
const FAKE = process.env.FAKE || '127.0.0.1:18099';
const [FAKE_HOST] = FAKE.split(':');
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
const body = () => page.locator('#plantWizBody');
const fb = () => page.locator('#plantWizFeedback').innerText();

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
  check('the wizard opens with four steps', await page.locator('#plantWizSteps .wizard-step').count() === 4);

  // ---- step 1 -------------------------------------------------------------
  check('how the datalogger is reached is a segmented choice with Both recommended',
        await body().locator('input[name="pwWays"]').count() === 3 && /recommended/i.test(await body().innerText()));
  await page.fill('#pwName', 'Sunfield PV');
  check('the id follows the name', (await page.locator('#pwId').inputValue()) === 'sunfield-pv');
  await page.fill('#pwId', 'sunfield');
  await page.click('#plantWizNext'); await page.waitForTimeout(400);
  check('no address → refused with a reason', /address/i.test(await fb()));
  // Solar API only: no Modbus port asked, no Modbus host required
  await body().locator('label.seg-btn').filter({ hasText: 'Solar API' }).click(); await page.waitForTimeout(400);
  check('Solar API only asks for no Modbus port', await page.locator('#pwPort').count() === 0);
  await page.fill('#pwHost', FAKE_HOST);
  // the stand-in listens on a port: the discover route takes the default 80 —
  // so the fixture's host carries the port in the address for the HTTP side
  await page.fill('#pwHost', FAKE);
  await page.fill('#pwRoot', 'pv');
  await page.click('#pwProbeBtn');
  await page.waitForFunction(() => /answered|no answer|error|fail/i.test(document.getElementById('pwProbeOut')?.innerText || ''), null, { timeout: 15000 });
  const probe = await page.locator('#pwProbeOut').innerText();
  check('Test says in words what answered and what it found', /Solar API[\s\S]*answered[\s\S]*3 inverters[\s\S]*1 meter/i.test(probe), probe.slice(0, 120));

  // ---- step 2: found on the datalogger ------------------------------------
  await page.click('#plantWizNext'); await page.waitForTimeout(1200);
  const s2 = await body().innerText();
  check('what was found is listed as ticks, not typed', /Found on the datalogger/i.test(s2) && /Inverters \(3\)/.test(s2) && /Site totals/.test(s2) && /Grid meter/.test(s2));
  check('the grid meter is offered unticked, with the reason', !(await body().locator('input[data-f="on"]').nth(2).isChecked()) && /Unticked on purpose/i.test(s2));
  check('no template dropdown for what was found', await body().locator('select[data-f="template"]').count() === 0);
  // a group by hand: the template list is filtered by role and transport
  await page.click('button:has-text("Add a group by hand")'); await page.waitForTimeout(500);
  const roleSel = body().locator('select[data-f="role"]').first();
  await roleSel.selectOption('inverter'); await page.waitForTimeout(400);
  const opts = await body().locator('select[data-f="template"] option').allInnerTexts();
  check('a manual inverter group over Solar API offers only HTTP inverter templates',
        opts.length >= 2 && opts.slice(1).every(o => /http/.test(o) && /inverter/i.test(o)) && !opts.some(o => /Janitza|Zigbee|BLE|SunSpec meter/.test(o)), opts.join(' | '));
  await body().locator('button[onclick^="app.plantWizRemoveGroup"]').last().click(); await page.waitForTimeout(400);
  await page.click('#plantWizNext'); await page.waitForTimeout(700);

  // ---- step 3: how often --------------------------------------------------
  const s3 = await body().innerText();
  check('intervals are labelled numbers, not group=seconds', /power, voltages, currents every/i.test(s3) && !/normal=/.test(s3));
  check('no Modbus rhythm asked for a Solar-API-only installation', !/complete reading/i.test(s3));
  await body().locator('input[data-f="httpEvery"]').first().fill('2');
  await page.click('#plantWizNext'); await page.waitForTimeout(700);

  // ---- step 4: review -----------------------------------------------------
  const review = await body().innerText();
  check('the review names what it holds and how it is read', /Inverters/.test(review) && /Site totals/.test(review) && /solar_api every 2 s/.test(review));
  check('it shows the real topics under the chosen root', /pv\/inverters\/N\//.test(review) && /pv\/site\//.test(review) && /pv\/inverters\/summary/.test(review));
  check('it says which devices will be created', /sunfield-u1/.test(review) && /sunfield-u3/.test(review) && /sunfield-site/.test(review));
  check('the button says what it will do', /create/i.test(await page.locator('#plantWizNextLabel').innerText()));

  // ---- create, then verify against the review -----------------------------
  await page.click('#plantWizNext');
  await page.waitForTimeout(4000);
  const d = await (await fetch(`${BASE}/api/endpoints/sunfield`)).json();
  check('the installation exists', d.id === 'sunfield', JSON.stringify(d).slice(0, 90));
  const g = Object.fromEntries((d.groups || []).map(x => [x.id, x]));
  check('inverters and site were created, the meter was not', !!g.inverters && !!g.site && !g.grid);
  check('the inverters are read over solar_api every 2 s with the right template',
        (g.inverters.sources || []).length === 1 && g.inverters.sources[0].id === 'solar_api'
        && g.inverters.sources[0].template === 'fronius_solar_api_inverter' && g.inverters.sources[0].poll_groups.realtime === 2,
        JSON.stringify(g.inverters.sources));
  check('the site is its own unit with the site template', g.site.total_units === 1 && g.site.sources[0].template === 'fronius_solar_api_site');
  check('units are the datalogger\'s ids', d.units.filter(u => u.group_id === 'inverters').map(u => u.unit_id).join(',') === '1,2,3');
  check('the topics are the ones the review showed',
        (g.inverters.outputs || {}).topic_prefix === 'pv/inverters/${unit_id}' && (g.site.outputs || {}).topic_prefix === 'pv/site'
        && g.inverters.topic === 'pv/inverters/summary', JSON.stringify([g.inverters.outputs, g.inverters.topic]));
  check('no Modbus fallback host was invented', !(d.connection || {}).host, JSON.stringify(d.connection));
  check('the wizard closed and landed on the installation', !(await page.locator('#plantWizardModal').isVisible()) && await page.locator('[data-endpoint-page]').count() === 1);

  check('no console errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) {
  check('script completed', false, String(e).slice(0, 300));
} finally {
  const ok = results.filter(r => r.ok).length;
  console.log(`\n${ok}/${results.length} checks passed`);
  await browser.close();
  process.exit(ok === results.length ? 0 : 1);
}
