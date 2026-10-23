// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The one connection form (ui/js/app-connform.js), in both its places — Add
// Device step 1 and the device page's Configure → Connection — as a user
// meets it: an untouched Save sends nothing, one edited field sends that
// field, a device leaves a bridge only when told, a change of how it is
// reached is said, HTTP and MQTT stay what they are, the gateway's refusals
// land at their field, one Test slot per form and a Test that carries what
// the form holds (vars, keepalive, certificate choice), labelled fields, no
// sideways scroll on a phone. Against the unchanged_save fixture (it edits
// the devices; restart the sandbox after).
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 OUT=<dir> node tools/e2e/connform_e2e.mjs
import { chromium } from 'playwright';
import { openDeviceTab } from './lib/device-page.mjs';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }, scheme = 'light') {
  const page = await browser.newPage({ viewport, colorScheme: scheme });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.reqs = [];
  page.on('request', r => { if (r.method() !== 'GET') page.reqs.push({ method: r.method(), url: r.url(), body: r.postData() }); });
  await watchDialogs(page);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const putsTo = (page, from, id) => page.reqs.slice(from).filter(r => r.method === 'PUT' && r.url.endsWith(`/api/devices/${id}`));
async function connTab(page, id) {
  await page.evaluate(i => window.app.openDeviceDetail(i), id);
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvConn fieldset', { state: 'attached' });
}
async function save(page) {
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForFunction(() => (document.getElementById('ddvFeedback')?.textContent || '').trim()
    || document.querySelector('#ddvConn [aria-invalid="true"]'), null, { timeout: 10000 });
  await page.waitForTimeout(300);
  return (await page.locator('#ddvFeedback').innerText().catch(() => '')).trim();
}

try {
  const page = await open();

  // ── an untouched Save sends nothing, on every way a device is reached ──
  for (const id of ['umg512', 'hp', 'rs', 'asc', 'udp', 'gx', 'gwm']) {
    await connTab(page, id);
    const n = page.reqs.length;
    const fb = await save(page);
    check(`${id}: an untouched Save sends nothing`, !putsTo(page, n, id).length && /Nothing to save/.test(fb), fb);
  }

  // ── one edited field sends that field (and the protocol it rides on) ──
  const edits = [
    ['udp', async () => page.fill('#ddvUnit', '9'), { unit_id: 9, protocol: 'udp' }],
    ['rs', async () => page.selectOption('#ddvBaud', '9600'), { baudrate: 9600, protocol: 'rtu' }],
    ['hp', async () => page.fill('#ddvTimeoutBr', '7'), { timeout: 7 }],
    ['gwm', async () => page.fill('#ddvUnit', '5'), { unit_id: 5 }],
    ['gx', async () => page.uncheck('#ddvMqttTlsInsecure'), { tls_insecure: false, protocol: 'mqtt' }],
    ['umg512', async () => page.fill('#ddvTimeout', '4'), null],
  ];
  for (const [id, edit, want] of edits) {
    await connTab(page, id);
    await edit();
    const n = page.reqs.length;
    const fb = await save(page);
    const sent = putsTo(page, n, id)[0];
    const conn = sent ? JSON.parse(sent.body).connection : null;
    if (want) check(`${id}: one edited field sends only that`, eq(conn, want), `${JSON.stringify(conn)} · ${fb}`);
    else check('umg512: the primary sends its address in full, Modbus TCP',
               conn && conn.protocol === 'tcp' && conn.timeout === 4 && 'host' in conn && !('bridge' in conn), JSON.stringify(conn));
    check(`${id}: saved`, /Saved/.test(fb), fb);
  }

  // ── a device leaves a bridge only when told, and says so ──
  await connTab(page, 'hp');
  check('on a bridge: the bridge and its bus are shown, no address', (await page.inputValue('#ddvBridge')) === 'ws'
        && (await page.locator('#ddvHost, #ddvRtuHost').count()) === 0);
  check('on a bridge: no route hint yet', !(await page.isVisible('#ddvRouteHint')));
  await page.click('#ddvConn label:has(input[name="ddvRtuMode"][value="rtu"])');
  check('direct serial: "starts afresh" is said', await page.isVisible('#ddvRouteHint'));
  await page.fill('#ddvSerial', '/dev/ttyFAKE5');
  {
    const n = page.reqs.length;
    const fb = await save(page);
    const conn = JSON.parse(putsTo(page, n, 'hp')[0]?.body || '{}').connection || {};
    check('leaving the bridge sends bridge: null with the whole serial connection',
          conn.bridge === null && conn.protocol === 'rtu' && conn.serial_port === '/dev/ttyFAKE5' && !('host' in conn), JSON.stringify(conn));
    const stored = (await (await fetch(`${BASE}/api/devices`)).json()).devices.find(d => d.id === 'hp');
    check('…and it is off the bridge, on that port', !stored.bridge && stored.protocol === 'rtu'
          && stored.serial?.serial_port === '/dev/ttyFAKE5', `${fb} · ${JSON.stringify(stored.serial)}`);
  }

  // ── a change of protocol is the whole connection, said beside it ──
  await connTab(page, 'udp');
  await page.uncheck('#ddvUdp');
  check('UDP off: "starts afresh" is said at once', await page.isVisible('#ddvRouteHint'));
  {
    const n = page.reqs.length;
    await save(page);
    const conn = JSON.parse(putsTo(page, n, 'udp')[0]?.body || '{}').connection || {};
    check('UDP off: a new protocol, the whole connection', conn.protocol === 'tcp' && conn.host && conn.port && conn.unit_id === 9, JSON.stringify(conn));
  }

  // ── HTTP and MQTT stay what they are; a device holding components stays as it is reached ──
  await connTab(page, 'gx');
  const dis = async v => page.isDisabled(`input[name="ddvProto"][value="${v}"]`);
  check('gx (holds components): every other way is held, with the reason',
        await dis('tcp') && await dis('rtu') && await dis('http') && /holds components/.test(await page.innerText('#ddvConn')));
  await connTab(page, 'web');
  check('web (HTTP): Modbus and MQTT are held', await dis('tcp') && await dis('mqtt') && !(await dis('http')));

  // ── refusals at their field: the form's own, then the gateway's ──
  await connTab(page, 'rs');
  await page.fill('#ddvUnitR', '300');
  {
    const n = page.reqs.length;
    await save(page);
    check('unit 300: said at the field, nothing sent', (await page.getAttribute('#ddvUnitR', 'aria-invalid')) === 'true'
          && !putsTo(page, n, 'rs').length);
  }
  await connTab(page, 'udp');
  await page.fill('#ddvHost', '/dev/ttyUSB0');
  {
    const n = page.reqs.length;
    await save(page);
    check('a serial path as an address: said at the host, nothing sent',
          (await page.getAttribute('#ddvHost', 'aria-invalid')) === 'true' && /serial port/.test(await page.innerText('#ddvConn'))
          && !putsTo(page, n, 'udp').length);
  }
  await connTab(page, 'web');
  await page.fill('#ddvUrl', 'http://127.0.0.2/x');
  {
    const n = page.reqs.length;
    await save(page);
    check("the gateway's refusal of a URL lands at the URL",
          putsTo(page, n, 'web').length === 1 && (await page.getAttribute('#ddvUrl', 'aria-invalid')) === 'true');
  }

  // ── Test carries what the form holds ──
  await connTab(page, 'gx');
  await page.fill('#ddvMqttVars', 'portal=c0ffee000001, vebus=275');
  await page.selectOption('#ddvKaMode', 'off');
  {
    const n = page.reqs.length;
    await page.click('#ddvConn [data-action="deviceDetailTest"]');
    await page.waitForFunction(() => (document.getElementById('ddvTestResult')?.textContent || '').trim(), null, { timeout: 20000 });
    const t = page.reqs.slice(n).find(r => r.method === 'POST' && /\/api\/devices\/test$/.test(r.url));
    const body = t ? JSON.parse(t.body) : {};
    check('an edited MQTT Test carries the vars, the keepalive and the certificate choice',
          body.device_id === 'gx' && body.connection?.vars?.vebus === '275' && body.connection?.keepalive === false
          && 'tls_insecure' in (body.connection || {}), t?.body?.slice(0, 300));
    check('one Test slot on the page', (await page.locator('#ddvConn .wiz-test-result').count()) === 1);
  }

  // ── accessibility: sections are fieldsets, every field is named ──
  for (const id of ['rs', 'hp', 'gx']) {
    await connTab(page, id);
    const unnamed = await page.evaluate(() => [...document.querySelectorAll('#ddvConn input, #ddvConn select')]
      .filter(el => el.type !== 'hidden' && el.getClientRects().length)
      .filter(el => !(el.labels && el.labels.length) && !el.getAttribute('aria-label') && !el.closest('label'))
      .map(el => el.id || el.name));
    check(`${id}: every field has a name`, !unnamed.length, unnamed.join(', '));
    check(`${id}: sections are fieldsets with a legend`, (await page.locator('#ddvConn fieldset > legend').count()) >= 2);
  }

  // ── Add Device: the same form, one Test slot, every mode ──
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#devWizConn fieldset');
  for (const v of ['tcp', 'rtu', 'http', 'mqtt']) {
    await page.click(`#deviceWizardModal label:has(input[name="devWizProto"][value="${v}"])`);
    check(`wizard ${v}: one Test slot`, (await page.locator('#devWizConn .wiz-test-result').count()) <= 1
          && (await page.locator('[id^="devWizTestResult"]').count()) <= 1);
    if (OUT) await page.screenshot({ path: `${OUT}/connform-wizard-${v}-desktop-light.png` });
  }
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));
  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── phone and dark: no sideways scroll; the pictures ──
  for (const [scheme, vp, tag] of [['light', { width: 390, height: 844 }, 'phone-light'], ['dark', { width: 390, height: 844 }, 'phone-dark'],
                                   ['dark', { width: 1440, height: 950 }, 'desktop-dark'], ['light', { width: 1440, height: 950 }, 'desktop-light']]) {
    const p = await open(vp, scheme);
    for (const id of ['rs', 'gx', 'gwm', 'web']) {
      await connTab(p, id);
      if (tag.startsWith('phone')) {
        const wide = await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
        check(`${id} on a phone (${scheme}): no sideways scroll`, !wide);
      }
      if (OUT) {
        await p.locator('#ddvConn').scrollIntoViewIfNeeded();
        await p.screenshot({ path: `${OUT}/connform-${id}-${tag}.png`, fullPage: false });
      }
    }
    check(`no page errors (${tag})`, p.errs.length === 0, p.errs.join(' | '));
    await p.close();
  }
} catch (e) {
  check('ran to the end', false, String(e).slice(0, 400));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
