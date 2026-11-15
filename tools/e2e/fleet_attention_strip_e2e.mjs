/* UX-04: the overview surfaces unresolved incidents BEFORE the full device census.
 * renderFleet prepends a "Needs attention" strip of the problem devices, each a
 * shortcut to the device, when not filtering (ui/js/app-fleet.js).
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node fleet_attention_strip_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  await page.waitForSelector('#fleetView', { timeout: 20000 });

  // drive controlled fleet data: one down device, one healthy
  const r = await page.evaluate(() => {
    app._fleetQuery = ''; app._fleetProblems = false;
    app.fleetData = { devices: [
      { id: 'bad1', name: 'Bad One', health: 'down', staleness_age_s: 1260, enabled: true, protocol: 'tcp', hero: [] },
      { id: 'ok1', name: 'OK One', health: 'ok', staleness_age_s: 2, enabled: true, protocol: 'tcp', hero: [] },
    ], endpoints: [] };
    app.renderFleet();
    const view = document.getElementById('fleetView');
    const attn = view.querySelector('.fleet-attn');
    const firstGroup = view.querySelector('.fleet-group');
    const order = attn && firstGroup ? (attn.compareDocumentPosition(firstGroup) & Node.DOCUMENT_POSITION_FOLLOWING) : 0;
    return { hasAttn: !!attn, namesBad: attn ? /Bad One/.test(attn.textContent) : false,
             excludesOk: attn ? !/OK One/.test(attn.textContent) : false,
             attnBeforeCensus: !!order, itemIsButton: !!(attn && attn.querySelector('button.fleet-attn-item[data-id="bad1"]')) };
  });
  check('an attention strip appears when there are problem devices', r.hasAttn);
  check('it lists the problem device (Bad One)', r.namesBad);
  check('it excludes the healthy device (OK One)', r.excludesOk);
  check('the strip comes BEFORE the device census', r.attnBeforeCensus);
  check('each attention item is a shortcut button to the device', r.itemIsButton);

  // clicking an attention item opens that device
  const opened = await page.evaluate(() => {
    let got = null; const orig = app.openFleetDevice; app.openFleetDevice = id => { got = id; };
    document.querySelector('.fleet-attn-item[data-id="bad1"]').click();
    app.openFleetDevice = orig;
    return got;
  });
  check('clicking an attention item opens that device', opened === 'bad1', opened);

  // with a filter active, the strip is suppressed (the filtered view is already focused)
  const filtered = await page.evaluate(() => {
    app._fleetProblems = true; app.renderFleet();
    const has = !!document.querySelector('#fleetView .fleet-attn');
    app._fleetProblems = false;
    return has;
  });
  check('the strip is suppressed while a filter is active', filtered === false);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
