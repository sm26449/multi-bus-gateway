// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The device pages as a user meets them (UI audit 2026-10-21, section C,
// lot a): each protocol said in its own words, a device page that stops what
// it started when another one opens, one Save for a device and its
// intervals, Unit ID and the gateway's refusals said at their field in the
// wizard, a bridge's move button opening the device's Edit tab, a
// component's page, deleting a parent with its components, and who drives a
// command's channel. Against the throwaway gateway of unchanged_save_e2e.mjs
// (its fixture, with a Modbus TCP gateway `gw` + `gwm` and an HTTP `web`).
// It deletes `udp` and edits `rs`: run tools/e2e reset (reset_fx.sh) after.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 OUT=<dir> node tools/e2e/devices_step3_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { deviceAction } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport, role) {
  const page = await browser.newPage({ viewport: viewport || { width: 1440, height: 950 } });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.dlg = await watchDialogs(page);
  page.reqs = [];
  page.on('request', r => page.reqs.push({ url: r.url(), method: r.method(), body: r.postData() }));
  // the sandbox runs without login: a role is what /api/auth/status says
  if (role) await page.route('**/api/auth/status', r => r.fulfill({ json: { enabled: true, role, modules: [] } }));
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const openDev = async (page, id, tab) => {
  await page.evaluate(([i, t]) => window.app.openDeviceDetail(i).then(() => t && window.app._switchDeviceTab(t)), [id, tab]);
  await page.waitForTimeout(700);
};
const rowText = (page, id) => page.locator(`#devicesListView .device-row:has(.dev-chip:text-is("${id}"))`).first().innerText();

try {
  const page = await open();
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForSelector('#devicesListView .device-row');

  // ── each protocol in its own words (D13, D14) ──
  const gx = await rowText(page, 'gx');
  check('list: an MQTT device names its broker and TLS, no unit, no poll/s',
        /none\.invalid:8883 · TLS/.test(gx) && !/unit \d/.test(gx) && !/poll\/s/.test(gx), gx);
  check('list: UDP is "Modbus UDP"', /Modbus UDP/.test(await rowText(page, 'udp')), await rowText(page, 'udp'));
  check('list: ASCII is "Modbus ASCII", with its line', /Modbus ASCII · \/dev\/ttyFAKE2 · 9600 8N1/.test(await rowText(page, 'asc')), await rowText(page, 'asc'));
  check('list: HTTP has no unit', !/unit \d/.test(await rowText(page, 'web')), await rowText(page, 'web'));

  await openDev(page, 'gx');
  const ov = await page.locator('#deviceDetailView [data-dpanel="overview"]').innerText();
  check('overview: MQTT source is its broker, never ":8883 · unit"', /none\.invalid:8883 · TLS/.test(ov) && !/· unit/.test(ov), ov.slice(0, 300));

  // ── the headline is the data's verdict (D15) ──
  await openDev(page, 'udp');
  const head = await page.locator('#deviceDetailView [data-verdict]').innerText();
  check('overview: a disabled UDP device reads "Disabled", not "Connected"', /Disabled/.test(head) && !/Connected/.test(head), head);
  check('overview: UDP has no "Link" fact (a datagram socket says nothing)', !/\bLink\b/.test(await page.locator('#deviceDetailView [data-dpanel="overview"]').innerText()));
  await openDev(page, 'gwm');
  const gwOv = await page.locator('#deviceDetailView [data-dpanel="overview"]').innerText();
  check('overview: Modbus TCP shows its link', /\bLink\b/.test(gwOv));
  check('overview: a device on a gateway names the gateway\'s bus', /gw · bus :15997 · unit 3/.test(gwOv), gwOv.slice(0, 300));

  // ── a device page stops what it started (D7) ──
  await openDev(page, 'rs', 'logs');
  await page.waitForTimeout(600);
  page.errs.length = 0;
  await openDev(page, 'gx');
  const t0 = page.reqs.length;
  await page.waitForTimeout(12000);
  const late = page.reqs.slice(t0).filter(r => /\/api\/devices\/rs\/events/.test(r.url));
  check('logs: switching device stops the old log poll', late.length === 0, `${late.length} polls of rs after the switch`);
  check('logs: no TypeError every 5 s', page.errs.length === 0, page.errs.join(' | '));
  await openDev(page, 'umg512', 'monitor');
  await openDev(page, 'gx');
  check('monitor: opening another device keeps the Monitor page', await page.locator('#monitorPage').count() === 1);
  // leaving a device's Monitor tab for the Monitor page (its own link, as
  // the dashboard's): the page is shown where it lives, and drawn
  await openDev(page, 'umg512', 'monitor');
  await page.evaluate(() => window.app.navigateTo('monitor', { fromNav: true }));
  await page.waitForTimeout(1500);
  const mon = await page.evaluate(() => {
    const el = document.getElementById('monitorPage');
    return { visible: !!(el && el.getClientRects().length && el.classList.contains('active')),
             home: !el?.closest('#deviceDetailView'), text: (el?.innerText || '').trim().length };
  });
  check('monitor: from a device\'s Monitor tab, the Monitor nav shows the page, drawn',
        mon.visible && mon.home && mon.text > 20, JSON.stringify(mon));
  await page.evaluate(() => window.app.navigateTo('devices', { fromNav: true }));
  await page.waitForTimeout(400);
  await openDev(page, 'rs', 'logs');
  await page.evaluate(() => window.app.navigateTo('dashboard'));
  const t1 = page.reqs.length;
  await page.waitForTimeout(6500);
  check('logs: leaving Devices stops the log poll',
        !page.reqs.slice(t1).some(r => /\/events\?/.test(r.url)));
  await page.evaluate(() => window.app.navigateTo('devices', { fromNav: true }));
  await page.waitForTimeout(500);

  // ── one Save for the device and its intervals (D8) ──
  await openDev(page, 'rs', 'edit');
  await page.waitForSelector('#ddvPg-normal');
  check('edit: no separate "Save intervals"', !(await page.locator('[data-action="saveDevicePollGroups"]').count()));
  await page.fill('#ddvPg-normal', 'abc');
  const p0 = page.reqs.length;
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(500);
  check('edit: a bad interval is said at its field, nothing is sent',
        (await page.getAttribute('#ddvPg-normal', 'aria-invalid')) === 'true'
        && !page.reqs.slice(p0).some(r => r.method !== 'GET' && /\/api\/devices\/rs/.test(r.url)));
  await page.fill('#ddvPg-normal', '7');
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1500);
  const pg = (await api('/api/devices/rs/poll-groups')).poll_groups;
  check('edit: Save & Apply applies the changed interval', pg.normal.interval === 7, JSON.stringify(pg.normal));
  check('edit: the other intervals and the descriptions are kept',
        pg.normal.description === 'system, freq, PF' && pg.realtime.interval === 1 && pg.slow.interval === 60, JSON.stringify(pg));
  const fb = await page.locator('#ddvFeedback').innerText();
  check('edit: an interval-only change says "Intervals applied", not "Nothing changed"',
        /Intervals applied/.test(fb) && !/Nothing changed/.test(fb), fb);
  await page.fill('#ddvPg-normal', '5');
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1200);
  // an untouched form sends nothing at all (FINDINGS PROD-1)
  const p1 = page.reqs.length;
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(800);
  check('edit: a save with no change says so and sends nothing',
        /Nothing to save/.test(await page.locator('#ddvFeedback').innerText())
        && !page.reqs.slice(p1).some(r => r.method !== 'GET' && /\/api\/devices\/rs/.test(r.url)),
        await page.locator('#ddvFeedback').innerText());

  // ── Unit ID: one rule, said at the field (D11) ──
  await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#devWizHost');
  await page.fill('#devWizHost', '127.0.0.1');
  await page.fill('#devWizUnit', '');
  const w0 = page.reqs.length;
  await page.evaluate(() => window.app.devWizardNext());
  await page.waitForTimeout(500);
  check('wizard: a blank Unit ID is said at the field, nothing sent',
        (await page.getAttribute('#devWizUnit', 'aria-invalid')) === 'true'
        && /0–255/.test(await page.locator('#devWizBody').innerText())
        && !page.reqs.slice(w0).some(r => r.method === 'POST' && /\/api\/devices/.test(r.url)));
  // unit 0 through a gateway is 0, not 1
  await page.selectOption('#devWizGw', 'gw::15998');
  await page.waitForTimeout(400);
  await page.fill('#devWizUnit', '0');
  const w1 = page.reqs.length;
  await page.evaluate(() => window.app.devWizardNext());
  await page.waitForTimeout(800);
  const sent = page.reqs.slice(w1).find(r => r.method === 'POST' && /\/api\/devices\?dry_run=true/.test(r.url));
  check('wizard: unit 0 through a gateway is sent as 0', sent && JSON.parse(sent.body).connection.unit_id === 0, sent?.body);
  check('wizard: a valid connection goes on to step 2', await page.evaluate(() => window.app._devWiz.step) === 2);

  // ── the gateway's refusal on step 1, at its field (D12) ──
  await page.evaluate(() => { window.app._devWiz.step = 1; Object.assign(window.app._devWiz.cf, { mode: 'rtu-tcp', bridge: '', bridge_port: null }); window.app._devWizRender(); });
  await page.waitForSelector('#devWizBridge');
  await page.fill('#devWizUnitBr', '2');                    // unit 2 on ws:15996 is hp's
  await page.evaluate(() => window.app.devWizardNext());
  await page.waitForTimeout(800);
  check('wizard: a unit already on that bus is refused on step 1, at the unit',
        await page.evaluate(() => window.app._devWiz.step) === 1
        && (await page.getAttribute('#devWizUnitBr', 'aria-invalid')) === 'true'
        && /already read by device 'hp'/.test(await page.locator('#devWizBody').innerText()));

  // ── no scan hint beside a bridge picker (D20) ──
  check('wizard: with bridges, "press Scan" is not shown', !(await page.locator('text=press Scan').isVisible()));
  await page.evaluate(() => window.app.closeModal('deviceWizardModal'));

  // ── the bridge card's move opens the device's Edit tab (D19) ──
  await page.evaluate(() => window.app.navigateTo('devices', { fromNav: true }));
  await page.waitForSelector('.bridge-card .bridge-move');
  await page.locator('.bridge-card[data-bridge="ws"] .bridge-move').first().click();
  await page.waitForTimeout(1000);
  check('bridge: move opens the device page on Edit, no wizard',
        await page.locator('#deviceWsTabs .config-main-tab.active[data-dtab="connection"]').count() === 1
        && !(await page.locator('#deviceWizardModal.active').count()));
  const busOpts = await page.locator('#ddvBridge option').allInnerTexts();
  check('edit: a bridged device can pick another bus of the same kind',
        busOpts.length >= 1 && busOpts.every(o => !/Meter gateway/.test(o)), busOpts.join(' | '));

  // ── a save with no change, on every kind of device (design check 5):
  // the page sends nothing; and the page's own payload, sent anyway, is
  // `unchanged` on the FIRST save even where the stored entry lacks keys the
  // form spells out (FINDINGS PROD-1, PROD-2) ──
  for (const id of ['umg512', 'hp', 'rs', 'asc', 'udp', 'gx', 'gwm']) {
    await openDev(page, id, 'edit');
    const p2 = page.reqs.length;
    await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
    await page.waitForTimeout(600);
    const fbText = await page.locator('#ddvFeedback').innerText();
    const sent = page.reqs.slice(p2).some(r => r.method === 'PUT' && r.url.endsWith(`/api/devices/${id}`));
    const first = await page.evaluate(async did => {
      const s = window.app._devDetail;
      const r = await fetch(`/api/devices/${encodeURIComponent(did)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(window.app._ddvPayload(did, s.data)) });
      return r.json();
    }, id);
    check(`unchanged save, ${id}: nothing sent; the form's payload is a no-op on the first save`,
          !sent && /Nothing to save/.test(fbText) && first.status === 'unchanged',
          `${fbText} / ${JSON.stringify(first).slice(0, 90)}`);
  }

  // ── an enabled device that is not running: Save offers Reconnect, and
  // only Reconnect sends (the PUT is answered here: nothing changes) ──
  await openDev(page, 'rs', 'edit');
  await page.evaluate(() => {
    const s = window.app._devDetail;
    document.getElementById('ddvEnabled').checked = true;
    s.orig.enabled = true;
    s.running = false;
  });
  let reconnectPut = 0;
  await page.route(/\/api\/devices\/rs$/, r => {
    if (r.request().method() !== 'PUT') return r.continue();
    reconnectPut++;
    return r.fulfill({ json: { status: 'updated', device: {} } });
  });
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(500);
  const offered = await page.locator('#ddvReconnect').isVisible();
  check('not running: Save sends nothing and offers Reconnect', offered && reconnectPut === 0,
        await page.locator('#ddvFeedback').innerText());
  await page.click('#ddvReconnect');
  await page.waitForTimeout(800);
  check('Reconnect sends the device again', reconnectPut === 1, String(reconnectPut));
  await page.unroute(/\/api\/devices\/rs$/);

  // ── an interval-only change on the primary meter: no device save, so no
  // reconnect (only the poll groups are sent) ──
  await openDev(page, 'umg512', 'edit');
  await page.waitForSelector('#deviceDetailView .ddv-pg');
  const pgId = await page.locator('#deviceDetailView .ddv-pg').first().getAttribute('id');
  const was = await page.inputValue(`#${pgId}`);
  await page.fill(`#${pgId}`, String(Number(was) + 1));
  const q0 = page.reqs.length;
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1500);
  const sentP = page.reqs.slice(q0).filter(r => r.method !== 'GET');
  check('primary: an interval-only change sends no device PUT, only the intervals',
        !sentP.some(r => r.method === 'PUT') && sentP.some(r => r.method === 'POST' && /\/poll-groups$/.test(r.url))
        && /Intervals applied/.test(await page.locator('#ddvFeedback').innerText()),
        sentP.map(r => `${r.method} ${r.url.replace(BASE, '')}`).join(', '));
  await page.fill(`#${pgId}`, was);
  await page.click('#deviceDetailView [data-action="saveDeviceDetail"]');
  await page.waitForTimeout(1200);

  // ── a component's page (D17) ──
  await openDev(page, 'gx-battery-288');
  // the parent is reached once: the breadcrumb, and one "Open device" in the ⋯ menu
  const inBody = await page.locator('#deviceDetailView button:visible:has-text("Open device")').count();
  const inMenu = await page.locator('#ddvActionsMenu [role=menuitem]:has-text("Open device")').count();
  check('component: one "Open device"', inBody === 0 && inMenu === 1, `${inBody}+${inMenu}`);
  check('component: its type by name', /Battery 288/.test(await page.locator('#deviceDetailView [data-component-of]').innerText()));
  await page.evaluate(() => window.app._switchDeviceTab('edit'));
  await page.waitForTimeout(300);
  page.dlg.answers.push('Shunt bank');
  await page.locator('#deviceDetailView [data-dpanel="connection"] [data-action="renameComponentUi"]').click();
  await page.waitForTimeout(1500);
  check('component: Rename in the page\'s dialog, the page shows the new name',
        page.dlg.native === 0 && /Shunt bank/.test(await page.locator('#deviceDetailView h2').innerText()));

  // ── deleting a parent names its components (D18) ──
  await openDev(page, 'gx');
  page.dlg.answers.push(false);
  await deviceAction(page, '[data-action="deleteDevice"]');
  await page.waitForTimeout(500);
  const seen = page.dlg.seen[page.dlg.seen.length - 1] || {};
  check('delete: a parent\'s dialog names its components', /component goes with it/.test((seen.details || []).join(' ')) && /Shunt bank/.test((seen.details || []).join(' ')),
        JSON.stringify(seen));
  check('delete: Cancel keeps it', (await api('/api/devices')).devices.some(d => d.id === 'gx'));

  // ── who drives a command (R-M9) ──
  await openDev(page, 'pv-u1', 'commands');
  await page.waitForSelector('.cmd-channels');
  const ch = await page.locator('.cmd-channels').innerText();
  check('commands: "Who drives it" names the armed rule and its layer', /ov-u1/.test(ch) && /safety/.test(ch), ch);
  // ov-u1 is a safety rule: the server never pauses it (pause_channel skips
  // priority ≥ 90) — the card says protection, not a pause
  check('commands: a safety owner is "protection — cannot be paused", no pause asked',
        /cannot be paused/i.test(await page.locator('.cmd-card[data-cmd="power_limit"] [data-cmd-protect]').innerText())
        && await page.locator('.cmd-card[data-cmd="power_limit"] .cmd-override').count() === 0,
        await page.locator('.cmd-card[data-cmd="power_limit"] .cmd-chan').innerHTML());
  await page.fill('#dcmd_power_limit_value', '50');
  page.dlg.answers.push(false);
  await page.locator('.cmd-card[data-cmd="power_limit"] [data-action="runDeviceCommand"]').first().click();
  await page.waitForTimeout(900);
  const runAsk = page.dlg.seen[page.dlg.seen.length - 1] || {};
  check('commands: Run\'s confirmation says the safety rule is not paused, never that it pauses',
        /not paused/.test(runAsk.message || '') && !/pauses for/.test(runAsk.message || ''), runAsk.message);
  const below = await page.evaluate(() => window.app._cmdChannelHtml({ name: 'x' }, { owner: 'r1', max_override_s: 3600,
      rules: [{ rule: 'r1', mode: 'armed', enabled: true, layer: 'schedule', priority: 50 }] }));
  check('commands: a rule under the safety layer is offered a pause', /cmd-override/.test(below) && /r1/.test(below));
  await openDev(page, 'pv-u2', 'commands');
  await page.waitForSelector('.dev-cmd-host .cmd-card');
  check('commands: no owner, no pause field', await page.locator('.cmd-card[data-cmd="power_limit"] .cmd-override').count() === 0);

  // ── delete from the device page leaves it (D6) ──
  await openDev(page, 'udp');
  page.dlg.answers.push(true);
  await deviceAction(page, '[data-action="deleteDevice"]');
  await page.waitForTimeout(1500);
  check('delete: back on the list, the row gone',
        await page.locator('#devicesListView').isVisible() && !(await page.locator('#deviceDetailView').isVisible())
        && !(await page.locator('#devicesListView .device-row[data-action="openDeviceDetail"] .dev-chip:text-is("udp")').count()));
  check('no native dialog', page.dlg.native === 0);
  check('no page errors', page.errs.length === 0, page.errs.join(' | '));
  await page.close();

  // ── what each role sees on a device page (design check 16) ──
  for (const role of ['viewer', 'operator']) {
    const rp = await open(null, role);
    await rp.evaluate(() => window.app.openDeviceDetail('pv-u1').then(() => window.app._switchDeviceTab('commands')));
    await rp.waitForSelector('.dev-cmd-host .cmd-card');
    const run = await rp.locator('.cmd-card [data-action="runDeviceCommand"]:visible').count();
    check(`${role}: Run ${role === 'operator' ? 'shown' : 'hidden'}`, role === 'operator' ? run > 0 : run === 0, String(run));
    await rp.evaluate(() => window.app.openDeviceDetail('gx-battery-288').then(() => window.app._switchDeviceTab('edit')));
    await rp.waitForTimeout(600);
    check(`${role}: no component Rename / Remove`, !(await rp.locator('#deviceDetailView [data-action="renameComponentUi"]:visible, #deviceDetailView [data-action="removeComponentUi"]:visible').count()));
    await rp.evaluate(() => window.app.openDeviceDetail('rs'));
    await rp.waitForTimeout(600);
    check(`${role}: no Save & Apply, no Delete`, !(await rp.locator('#deviceDetailView [data-action="saveDeviceDetail"]:visible, #deviceDetailView [data-action="deleteDevice"]:visible').count()));
    check(`no page errors (${role})`, rp.errs.length === 0, rp.errs.join(' | '));
    await rp.close();
  }

  // ── on a phone: bridge fields full width (D20) ──
  const phone = await open({ width: 390, height: 844 });
  await phone.evaluate(() => window.app.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: 'ws', bridge_port: 15996, unit_id: 4 }));
  await phone.waitForSelector('#devWizBridge');
  const w = await phone.evaluate(() => document.getElementById('devWizBridge').getBoundingClientRect().width);
  check('phone: the bridge picker is full width', w >= 300, String(w));
  if (OUT) await phone.screenshot({ path: `${OUT}/devices-phone-bridge.png` });
  await phone.close();
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
