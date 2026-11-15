/* Lot E (ADR-0043): the Settings → Alerts tab lets an operator point MBG at the
 * alertd notification service with a simple form — enable, fire URL, API key
 * (stored masked), source — applied live on save, round-tripping the masked key.
 *
 *   tools/e2e/fixture_up.sh plant 18085
 *   MBG_URL=http://127.0.0.1:18085 CHROMIUM_PATH=<chrome> node alertd_settings_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18085';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

const getCfg = (page) => page.evaluate(() => fetch('/api/config/alerts').then(r => r.json()));

try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});

  // Settings → Alerts tab
  await page.goto(BASE + '/#/config'); await page.waitForTimeout(400);
  await page.click('[data-cfgtab="alerts"]'); await page.waitForTimeout(400);
  check('the alertd section fields are present', await page.evaluate(() =>
    !!document.getElementById('cfgAlertdEnabled') && !!document.getElementById('cfgAlertdUrl')
    && !!document.getElementById('cfgAlertdKey') && !!document.getElementById('cfgAlertdSource')));
  check('API key field is a password input (not shown in clear)', await page.evaluate(() =>
    document.getElementById('cfgAlertdKey').type === 'password'));

  // fill + save
  await page.check('#cfgAlertdEnabled');
  await page.fill('#cfgAlertdUrl', 'http://klopot:5090/v1/fire');
  await page.fill('#cfgAlertdKey', 'alertd_e2e_secret');
  await page.fill('#cfgAlertdSource', 'mbg-e2e');
  await page.click('[data-action="saveAlerts"]');
  await page.waitForTimeout(700);

  let cfg = await getCfg(page);
  check('save persisted the alertd block', !!(cfg.alertd && cfg.alertd.enabled
    && cfg.alertd.url === 'http://klopot:5090/v1/fire' && cfg.alertd.source === 'mbg-e2e'));
  check('the API key is returned masked, never in clear', cfg.alertd.api_key === '••••••', cfg.alertd.api_key);

  // reload the tab: the form shows the masked key, and re-saving keeps the secret
  await page.goto(BASE + '/#/devices'); await page.waitForTimeout(200);
  await page.goto(BASE + '/#/config'); await page.waitForTimeout(300);
  await page.click('[data-cfgtab="alerts"]'); await page.waitForTimeout(400);
  check('reloaded form shows enable + url + masked key', await page.evaluate(() =>
    document.getElementById('cfgAlertdEnabled').checked
    && document.getElementById('cfgAlertdUrl').value === 'http://klopot:5090/v1/fire'
    && document.getElementById('cfgAlertdKey').value === '••••••'));

  // change the source only, save with the masked key untouched → key preserved
  await page.fill('#cfgAlertdSource', 'mbg-edge');
  await page.click('[data-action="saveAlerts"]'); await page.waitForTimeout(700);
  cfg = await getCfg(page);
  check('re-saving from the masked view keeps the key and applies the change',
    cfg.alertd.api_key === '••••••' && cfg.alertd.source === 'mbg-edge');

  // the Download-pack link serves an importable YAML pack with MBG's alert templates
  check('the Download alertd pack link is present', await page.evaluate(() =>
    !!document.getElementById('cfgAlertdPackLink')
    && document.getElementById('cfgAlertdPackLink').getAttribute('href') === '/api/alerts/alertd-pack'));
  const pack = await page.evaluate(() => fetch('/api/alerts/alertd-pack')
    .then(r => Promise.all([r.status, r.headers.get('content-type') || '', r.text()])));
  check('pack endpoint returns YAML with MBG templates', pack[0] === 200
    && /yaml/.test(pack[1]) && /type:\s*mbg_device/.test(pack[2]) && /templates:/.test(pack[2]),
    [pack[0], pack[1], pack[2].slice(0, 40)]);

  // (the shared Test button's alertd result is covered by the AlertManager.test()
  // unit test — /api/alerts/test needs a credential, which this throwaway box has none of.)

  // Lot D: alertd surfaces in the status payload + on the Status page once armed.
  await page.check('#cfgAlertsEnabled'); await page.click('[data-action="saveAlerts"]'); await page.waitForTimeout(700);
  const st = await page.evaluate(() => fetch('/api/alerts').then(r => r.json()).then(j => j.status));
  check('status payload: alertd active + listed as a channel', !!(st && st.alertd && st.alertd.active
    && (st.channels || []).includes('alertd')), { alertd: st && st.alertd, channels: st && st.channels });

  await page.goto(BASE + '/#/status'); await page.waitForTimeout(600);
  await page.click('#stTab-outputs').catch(() => {});   // the alerts card lives on the Outputs tab
  const shown = await page.waitForFunction(
    () => /v1\/fire/.test(document.getElementById('stAlertBody')?.textContent || ''),
    { timeout: 8000 }).then(() => true).catch(() => false);
  check('Status page shows the alertd destination + its URL', shown,
    await page.evaluate(() => (document.getElementById('stAlertBody')?.textContent || '').slice(0, 120)));

  await ctx.close();
} catch (e) { check('ran', false, String(e).slice(0, 220)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
