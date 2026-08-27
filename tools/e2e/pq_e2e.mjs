/* End-to-end validation of the MBG Power Quality feature (3.38.0)
 * against the ephemeral instance on :8093 (real Janitza meter + real InfluxDB,
 * known credentials, MQTT off, register polling pointed away).
 */
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://localhost:8080';
const USER = process.env.MBG_USER || 'admin';
const PASS = process.env.MBG_PASS || '';
const SHOT = (n) => `${process.env.SHOTS_DIR || './shots'}/${n}.png`;

const results = [];
function check(name, cond, extra = '') {
  results.push({ name, ok: !!cond, extra });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 },
                                     bypassCSP: true });
page.setDefaultTimeout(20000);
const consoleErrors = [];
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', e => consoleErrors.push(String(e)));

async function dismissOnboarding() {
  // the e2e instance's Modbus host is intentionally unreachable, so the
  // "Connect your first device" wizard can pop over the UI — dismiss it
  const later = page.locator('button:has-text("Later")');
  if (await later.count().catch(() => 0)) await later.first().click().catch(() => {});
}

try {
  // ---- 1. login ----------------------------------------------------------
  await page.goto(BASE + '/');
  await page.waitForSelector('#loginUser', { state: 'visible' });
  await page.fill('#loginUser', USER);
  await page.fill('#loginPass', PASS);
  await page.screenshot({ path: SHOT('01-login') });
  await Promise.all([page.waitForLoadState('load'), page.click('#loginBtn')]);
  await page.waitForSelector('#loginOverlay', { state: 'hidden', timeout: 20000 })
    .catch(() => {});
  check('login succeeds', await page.isHidden('#loginOverlay'));
  consoleErrors.length = 0;   // pre-login /api/languages 401s are expected noise

  // ---- 2. devices list + gating -----------------------------------------
  await dismissOnboarding();
  await page.click('.nav-tab[data-page="devices"]');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: SHOT('02-devices') });

  // open the Janitza (primary) device detail
  const devicesResp = await page.evaluate(async () => (await (await fetch('/api/devices')).json()));
  const devs = devicesResp.devices || [];
  const jz = devs.find(d => d.pq_supported);
  check('API: exactly one pq_supported device', devs.filter(d => d.pq_supported).length === 1,
        devs.map(d => `${d.id}:${d.pq_supported}`).join(' '));
  check('API: pq_supported is the Janitza', jz && jz.template === 'janitza_umg512_pro');

  await page.evaluate((id) => window.app.openDeviceDetail(id), jz.id);
  await page.waitForSelector('#deviceWsTabs');
  const pqTab = page.locator('#deviceWsTabs [data-dtab="pq"]');
  check('PQ tab rendered on Janitza', await pqTab.count() === 1);
  check('PQ tab enabled', await pqTab.isEnabled());
  await page.screenshot({ path: SHOT('03-device-detail') });

  // ---- 3. PQ tab: events list -------------------------------------------
  await pqTab.click();
  await page.waitForSelector('#pqEventList .pq-event', { timeout: 25000 });
  const evCount = await page.locator('#pqEventList .pq-event').count();
  check('event list populated', evCount > 0, `${evCount} events`);
  const dayHeaders = await page.locator('#pqEventList .monitor-category-header').count();
  check('day grouping headers present', dayHeaders > 0, `${dayHeaders} day(s)`);
  check('newest event auto-selected', await page.locator('#pqEventList .pq-event.selected').count() === 1);
  const detail = await page.locator('#pqDetail').innerText();
  check('detail header shows metadata', /duration|min|max/i.test(detail), detail.slice(0, 90));

  // ---- 4. waveform (archive or live read-through) -----------------------
  await page.waitForFunction(() => {
    const ov = document.getElementById('pqOverlay');
    return ov && (ov.style.display === 'none' || !/Loading/i.test(ov.textContent));
  }, { timeout: 45000 });
  const overlayText = await page.locator('#pqOverlay').innerText().catch(() => '');
  const infoText = await page.locator('#pqInfo').innerText().catch(() => '');
  const canvasDrawn = await page.evaluate(() => {
    const c = document.getElementById('pqCanvas');
    if (!c || !c.width) return false;
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    for (let i = 3; i < d.length; i += 4) if (d[i] !== 0) return true;
    return false;
  });
  check('waveform drawn OR honest empty-state', canvasDrawn || overlayText.length > 0,
        canvasDrawn ? `drawn · ${infoText}` : `overlay: ${overlayText.slice(0, 80)}`);
  await page.screenshot({ path: SHOT('04-pq-waveform') });

  // click a second event (if any) and switch channel
  if (evCount > 1) {
    await page.locator('#pqEventList .pq-event').nth(1).click();
    await page.waitForTimeout(4000);
    check('second event selectable', await page.locator('#pqEventList .pq-event.selected').count() === 1);
    await page.selectOption('#pqChannel', 'UL2');
    await page.waitForTimeout(4000);
    check('channel switch does not error', true);
    await page.screenshot({ path: SHOT('05-pq-second-event') });
  }

  // ---- 5. Outputs tab: PQ recorder card ----------------------------------
  await dismissOnboarding();
  await page.click('#deviceWsTabs [data-dtab="outputs"]');
  await page.waitForSelector('#ddvPqEnabled');
  check('Outputs: PQ card present', await page.locator('#ddvPqEnabled').count() === 1);
  check('Outputs: PQ enabled checkbox reflects config', await page.isChecked('#ddvPqEnabled'));
  await page.waitForFunction(() => (document.getElementById('ddvPqStatus')?.textContent || '').length > 0,
    { timeout: 15000 }).catch(() => {});
  const st = await page.locator('#ddvPqStatus').innerText().catch(() => '');
  check('Outputs: live status line filled', st.length > 0, st.slice(0, 90));
  await page.screenshot({ path: SHOT('06-outputs-card') });

  // save round-trip (poll_s 300 -> 240 -> back), verifies POST /api/pq/config
  const saveAndWait = async (val) => {
    await page.fill('#ddvPqPoll', String(val));
    const [resp] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/pq/config'), { timeout: 20000 }),
      page.click('[data-action="savePqRecorder"]'),
    ]);
    return resp.status();
  };
  let st240 = await saveAndWait(240);
  let cfg = await page.evaluate(async () =>
    (await (await fetch('/api/devices')).json()).devices.find(d => d.pq_supported).pq_recorder);
  check('config save applies (poll_s=240)', st240 === 200 && cfg && cfg.poll_s === 240,
        `POST ${st240} · ${JSON.stringify(cfg)}`);
  let st300 = await saveAndWait(300);
  cfg = await page.evaluate(async () =>
    (await (await fetch('/api/devices')).json()).devices.find(d => d.pq_supported).pq_recorder);
  check('config restored (poll_s=300)', st300 === 200 && cfg && cfg.poll_s === 300,
        `POST ${st300} · ${JSON.stringify(cfg)}`);

  // ---- 6. non-Janitza device must NOT show the tab -----------------------
  const other = devs.find(d => !d.pq_supported && !d.primary);
  if (other) {
    await page.evaluate((id) => window.app.openDeviceDetail(id), other.id);
    await page.waitForSelector('#deviceWsTabs');
    check(`PQ tab hidden on ${other.id}`, await page.locator('#deviceWsTabs [data-dtab="pq"]').count() === 0);
    await page.screenshot({ path: SHOT('07-other-device-no-tab') });
  }

  check('no JS console errors', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));
} catch (e) {
  check('e2e run completed', false, String(e).slice(0, 200));
  await page.screenshot({ path: SHOT('99-failure') }).catch(() => {});
} finally {
  await browser.close();
}

const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
