/* UX-05: a device's overview describes the DEVICE first — plain state, then the
 * live values — and moves the transport/template/topic engineering facts into a
 * collapsed "Technical details" summary below (ui/js/app-devices.js
 * _deviceOverviewHtml, standalone branch).
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node device_overview_order_e2e.mjs
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

  const r = await page.evaluate(() => {
    app._devDetail = { sinks: { mqtt: {}, influxdb: {} } };
    const d = { protocol: 'rtu-tcp', template: 'samsung-dvm', selected_registers: 44,
                topic_prefix: 'mbg/devices/samsung-dvm', bucket: 'samsung-dvm', mqtt_enabled: true, influxdb_enabled: true };
    const entry = { data_health: 'ok', connected: true, enabled: true, type: 'heatpump', kind: 'heatpump',
                    kind_source: 'template', staleness_age_s: 3, poll_rate: 0.5, read_via: [] };
    const html = app._deviceOverviewHtml(d, entry);
    const iSnap = html.indexOf('id="ddvSnapshot"');
    const iTech = html.indexOf('ddv-tech');
    const iSource = html.indexOf('Source');
    return { hasSnap: iSnap >= 0, hasTechDetails: /<details class="ddv-tech">/.test(html),
             snapBeforeTech: iSnap >= 0 && iTech >= 0 && iSnap < iTech,
             sourceInsideTech: iSource > iTech && iTech >= 0,
             techHasFacts: /Template/.test(html) && /Measurements polled/.test(html) };
  });

  check('the live-values region (#ddvSnapshot) is present', r.hasSnap);
  check('a collapsed "Technical details" <details> exists', r.hasTechDetails);
  check('the live values come BEFORE the technical details', r.snapBeforeTech);
  check('the Source/transport facts live inside the technical summary', r.sourceInsideTech);
  check('the technical summary still holds template + poll facts', r.techHasFacts);

  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
