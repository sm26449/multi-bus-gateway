/* FUXA, as a person sees it, after tools/fuxa/provision.py: the gateway's
 * connection is on the Connections page and green (connected), its tag list
 * holds every provisioned tag, and the values have arrived (a timestamp on
 * each row of the first page); the home screen made by tools/fuxa/screen.py
 * shows a live value in every row (FUXA_SCREEN=0 skips it). The values
 * themselves are compared with the gateway by tools/fuxa/compare.py.
 *
 *   FUXA_URL=http://127.0.0.1:1881 FUXA_CONN=MBG SHOT=<dir> CHROMIUM_PATH=<chrome> node fuxa_ui_check.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.FUXA_URL || 'http://127.0.0.1:1881';
const CONN = process.env.FUXA_CONN || 'MBG';
const SHOT = process.env.SHOT;
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
page.setDefaultTimeout(20000);
try {
  const project = await (await page.request.get(BASE + '/api/project')).json();
  const dev = Object.values(project.devices || {}).find(d => d.name === CONN);
  check(`FUXA has the connection '${CONN}'`, dev);
  const nTags = Object.keys(dev?.tags || {}).length;
  await page.goto(BASE + '/device'); await page.waitForLoadState('networkidle');
  const wiz = page.getByRole('button', { name: 'CLOSE', exact: true });    // FUXA's first-run security dialog
  if (await wiz.count()) await wiz.click();
  const card = page.locator('.node-device', { hasText: CONN });
  let colour = '';
  for (let i = 0; i < 20 && colour !== 'rgb(0, 176, 80)'; i++) {
    await page.waitForTimeout(500);
    colour = await card.locator('.device-status').evaluate(e => getComputedStyle(e).backgroundColor).catch(() => '');
  }
  check('the connection is green (connected)', colour === 'rgb(0, 176, 80)', colour);
  if (SHOT) await page.screenshot({ path: `${SHOT}/fuxa-connections.png` });
  await card.locator('button.device-list').click();
  await page.waitForTimeout(2500);
  const range = await page.locator('.mat-mdc-paginator-range-label').innerText();
  check(`the tag list holds all ${nTags} tags`, new RegExp(`of ${nTags}$`).test(range.trim()), range);
  const rows = page.locator('mat-row, tr.mat-mdc-row');
  const n = await rows.count();
  // the editor fills the values as FUXA pushes them: give a large project time
  let stamped = 0;
  for (let i = 0; i < 30 && stamped < n; i++) {
    if (i) await page.waitForTimeout(1000);
    stamped = await rows.evaluateAll(rs => rs.filter(r => /\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}/.test(r.innerText)).length);
  }
  check('every row on the page has received a value', n > 0 && stamped === n, `${stamped}/${n}`);
  if (SHOT) await page.screenshot({ path: `${SHOT}/fuxa-tags.png` });
  if (process.env.FUXA_SCREEN !== '0') {                 // the screen tools/fuxa/screen.py made
    await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
    const vals = page.locator('g[type="svg-ext-value"] text');
    let shown = 0, total = 0;
    for (let i = 0; i < 20 && (total === 0 || shown < total); i++) {
      await page.waitForTimeout(1000);
      const t = await vals.evaluateAll(es => es.map(e => e.textContent || "")); total = t.length; shown = t.filter(x => x.trim() && x.trim() !== '##.##').length;
    }
    check('the home screen shows a live value in every row', total > 0 && shown === total, `${shown}/${total}`);
    if (SHOT) await page.screenshot({ path: `${SHOT}/fuxa-home.png`, fullPage: true });
  }
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
