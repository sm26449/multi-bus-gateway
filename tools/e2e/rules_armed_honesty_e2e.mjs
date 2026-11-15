/* UX-02: an "Armed" badge is a configured mode, not proof that protection is
 * operating. app._ruleCardHtml must show "Armed · input unavailable" (amber), not
 * a reassuring green "Armed", when an armed device-reading rule has no current
 * input — its signal is null (source asleep), it cannot run, or its device is
 * absent (ui/js/app-rules.js). A rule with a live signal keeps a green "Armed".
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node rules_armed_honesty_e2e.mjs
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
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});

  // extract only the header pill area: the first .sink-pill in the card header
  const pill = r => page.evaluate(rule => {
    const html = app._ruleCardHtml(rule, false);
    const m = html.match(/<span class="sink-pill ([^"]*)"[^>]*>([^<]*)<\/span>/);
    return m ? { cls: m[1], text: m[2].trim() } : null;
  }, r);
  const base = { id: 'ov-u1', label: 'Over-voltage u1', kind: 'steps', enabled: true,
                 signal: 'max(pv-u1.voltage_l1_n)', signal_unit: 'V', inputs: [],
                 steps: [{ label: 'trim', at: 253, value: 80 }], release_below: 250, normal: { value: 100 } };

  // armed + a live signal → green "Armed"
  let p = await pill({ ...base, mode: 'armed', live: { signal: 246.2, state: 'normal', units: {} } });
  check('armed with a live signal shows green "Armed"', /\bok\b/.test(p.cls) && /^Armed$/.test(p.text), p);

  // armed + signal null (source asleep) → amber "Armed · input unavailable"
  p = await pill({ ...base, mode: 'armed', live: { signal: null, state: 'normal', units: {} } });
  check('armed with no signal (asleep) shows amber "Armed · input unavailable"',
        /warn/.test(p.cls) && !/\bok\b/.test(p.cls) && /input unavailable/.test(p.text), p);

  // armed + cannot run (lv.error) → amber "Armed · input unavailable"
  p = await pill({ ...base, mode: 'armed', live: { signal: null, error: 'bad expression', units: {} } });
  check('armed that cannot run shows amber "Armed · input unavailable"',
        /warn/.test(p.cls) && /input unavailable/.test(p.text), p);

  // armed + the input device is absent (no_device) → amber "Armed · input unavailable"
  p = await pill({ ...base, mode: 'armed', inputs: [{ problem: 'no_device', ref: 'pv-u1' }],
                   live: { signal: null, units: {} } });
  check('armed reading an absent device shows amber "Armed · input unavailable"',
        /warn/.test(p.cls) && /input unavailable/.test(p.text), p);

  // shadow → "Shadow" (unchanged)
  p = await pill({ ...base, mode: 'shadow', live: { signal: null, units: {} } });
  check('a shadow rule still shows "Shadow"', /^Shadow$/.test(p.text), p);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(r => !r.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
