// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The device pages as a user meets them (UI audit 2026-10-21, section C,
// lot a): each protocol said in its own words, a device page that stops what
// it started when another one opens, one Save (the Edit modal's) for a device and its
// intervals, Unit ID and the gateway's refusals said at their field in the
// wizard, a bridge's move button opening the device's Edit tab, a
// component's page, deleting a parent with its components, and who drives a
// command's channel. Against the throwaway gateway of unchanged_save_e2e.mjs
// (its fixture, with a Modbus TCP gateway `gw` + `gwm` and an HTTP `web`).
// It deletes `udp` and edits `rs`: run tools/e2e reset (reset_fx.sh) after.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 OUT=<dir> node tools/e2e/devices_step3_e2e.mjs
import { chromium } from 'playwright';
import { openAdd, answer, fill, step } from './lib/add-device.mjs';
import { watchDialogs } from './lib/dialogs.mjs';
import { deviceAction, editorSection, editorSettled, editorModalOpen, saveDeviceEditor, closeDeviceEditor } from './lib/device-page.mjs';

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
// `edit`: the connection editor, from ⋯ (L3.3: no longer a tab)
const openDev = async (page, id, tab) => {
  await page.evaluate(([i, t]) => window.app.openDeviceDetail(i).then(() => {
    if (!t) return;
    if (t === 'edit' || t === 'connection') return window.app.ddvEdit('connection');
    return window.app._switchDeviceTab(t);
  }), [id, tab]);
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
  await openDev(page, 'gx', 'setup');
  await page.waitForSelector('[data-dpanel="setup"] [data-setup="connection"]');
  const gxSetup = await page.locator('[data-dpanel="setup"] [data-setup="connection"]').innerText();
  check('setup: the MQTT broker with its port and TLS', /none\.invalid:8883 · TLS/.test(gxSetup), gxSetup.slice(0, 300));

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
  // what the page shows before the edit: the descriptions it must keep (the
  // fixture's rs has no registers file, so they are the gateway's groups)
  // (L3.4: the Edit modal's Polling section; the one Save is the modal's,
  // its dry run judges an interval before anything is saved)
  const pgBefore = (await api('/api/devices/rs/poll-groups')).poll_groups;
  // a save of rs, never its dry run
  const savesOf = (from, id) => page.reqs.slice(from).filter(r => ['PUT', 'PATCH', 'POST'].includes(r.method)
    && new RegExp(`/api/devices/${id}(/poll-groups)?$`).test(new URL(r.url).pathname) && !/dry_run/.test(r.url));
  await openDev(page, 'rs', 'edit');
  await editorSection(page, 'polling');
  await page.waitForSelector('#ddvPg-normal');
  check('edit: no separate "Save intervals"', !(await page.locator('[data-action="saveDevicePollGroups"]').count()));
  await page.fill('#ddvPg-normal', 'abc');
  const p0 = page.reqs.length;
  await editorSettled(page);
  await page.click('#devEditSave', { force: true });
  await page.waitForTimeout(500);
  check('edit: a bad interval is said at its field, nothing is saved',
        (await page.getAttribute('#ddvPg-normal', 'aria-invalid')) === 'true' && !savesOf(p0, 'rs').length);
  await page.fill('#ddvPg-normal', '7');
  await editorSettled(page);
  check('edit: an interval-only change says it applies live',
        /1 change · applies live/.test(await page.locator('#devEditSummary').innerText()), await page.locator('#devEditSummary').innerText());
  const r7 = await saveDeviceEditor(page);
  const pg = (await api('/api/devices/rs/poll-groups')).poll_groups;
  check('edit: Save applies the changed interval, in one PATCH, no restart', r7?.ok() && (await r7.json()).restarted === false
        && pg.normal.interval === 7, JSON.stringify(pg.normal));
  check('edit: the other intervals and the descriptions are kept',
        pg.normal.description === pgBefore.normal.description && !!pg.normal.description
        && pg.realtime.interval === 1 && pg.slow.interval === 60, JSON.stringify(pg));
  await openDev(page, 'rs', 'edit');
  await editorSection(page, 'polling');
  await page.fill('#ddvPg-normal', '5');
  await saveDeviceEditor(page);
  // an untouched form sends nothing at all (FINDINGS PROD-1)
  await openDev(page, 'rs', 'edit');
  const p1 = page.reqs.length;
  await editorSettled(page);
  await page.click('#devEditSave', { force: true });
  await page.waitForTimeout(800);
  check('edit: a save with no change says so and sends nothing',
        /No changes/.test(await page.locator('#devEditSummary').innerText())
        && !page.reqs.slice(p1).some(r => r.method !== 'GET' && /\/api\/devices\/rs/.test(r.url)),
        await page.locator('#devEditSummary').innerText());
  await closeDeviceEditor(page);

  // ── Unit ID: one rule, said at the field (D11) — Add's Connection step (L3.9) ──
  await openAdd(page);
  await page.click('[data-ad-way="connection"]');
  await answer(page, 'PortKind', 'ethernet');
  await answer(page, 'Reach', 'address').catch(() => {});
  await fill(page, 'Host', '127.0.0.1');
  await page.fill('#csAddUnit', '');
  const w0 = page.reqs.length;
  await page.click('#addDevNext');
  await page.waitForTimeout(500);
  check('Add: a blank Unit ID is said at the field, nothing sent',
        (await page.getAttribute('#csAddUnit', 'aria-invalid')) === 'true'
        && /0–255/.test(await page.locator('#addDeviceContent').innerText())
        && !page.reqs.slice(w0).some(r => r.method === 'POST' && /\/api\/devices/.test(r.url)));
  // unit 0 through a gateway is 0, not 1
  await answer(page, 'Reach', 'bridge');
  await page.selectOption('#csAddBus', 'gw::15998');
  await page.waitForTimeout(400);
  await page.fill('#csAddUnit', '0');
  await page.locator('#csAddUnit').blur();
  await answer(page, 'Role', 'poll').catch(() => {});
  const w1 = page.reqs.length;
  await page.click('#addDevNext');
  await page.waitForTimeout(800);
  const sent = page.reqs.slice(w1).find(r => r.method === 'POST' && /\/api\/devices\?dry_run=true/.test(r.url));
  check('Add: unit 0 through a gateway is sent as 0', sent && JSON.parse(sent.body).connection.unit_id === 0, sent?.body);
  check('Add: a valid connection goes on (By connection: the model next)', await step(page) === 'what');

  // ── the gateway's refusal on the Connection step, at its field (D12) ──
  await page.click('#addDevBack');
  await answer(page, 'PortKind', 'serial');
  await answer(page, 'Reach', 'bridge');
  await page.selectOption('#csAddBus', 'ws::15996');
  await page.waitForTimeout(400);
  await answer(page, 'Role', 'poll').catch(() => {});
  await page.fill('#csAddUnit', '2');                       // unit 2 on ws:15996 is hp's
  await page.locator('#csAddUnit').blur();
  await page.click('#addDevNext');
  await page.waitForTimeout(800);
  check('Add: a unit already on that bus is refused on the Connection step, at the unit',
        await step(page) === 'connection'
        && (await page.getAttribute('#csAddUnit', 'aria-invalid')) === 'true'
        && /already read by device 'hp'/.test(await page.locator('#addDeviceContent').innerText()));

  // ── no scan hint beside a bridge picker (D20) ──
  check('Add: with bridges, "press Scan" is not shown', !(await page.locator('text=press Scan').isVisible()));
  await page.evaluate(() => { window.app._ad.done = true; window.app.closeModal('addDeviceModal'); });

  // ── a bridge card's device: its ⋯ → Edit connection… (D19; L3.3: the
  // device page's own menu, one builder) opens its connection editor ──
  await page.evaluate(() => window.app.navigateTo('devices', { fromNav: true }));
  await page.waitForSelector('.bridge-card [data-dev-menu]');
  await page.locator('.bridge-card[data-bridge="ws"] [data-dev-menu]').first().click();
  await page.locator('.bridge-card[data-bridge="ws"] .mbg-menu:not([hidden]) [data-ddv-act="connection"]').click();
  await page.waitForTimeout(1000);
  check('bridge: Edit connection… opens the device page and its Edit modal at Connection, no wizard',
        await page.locator('#devEditModal.active').count() === 1 && await page.locator('#ddvConn').isVisible()
        && !(await page.locator('#addDeviceModal.active').count()));
  const busOpts = await page.locator('#ddvBus option').allInnerTexts();
  check('edit: a bridged device can pick another bus of the same kind',
        busOpts.length >= 1 && busOpts.every(o => !/Meter gateway/.test(o)), busOpts.join(' | '));

  // ── a save with no change, on every kind of device (design check 5):
  // the page sends nothing; and the page's own payload, sent anyway, is
  // `unchanged` on the FIRST save even where the stored entry lacks keys the
  // form spells out (FINDINGS PROD-1, PROD-2) ──
  for (const id of ['umg512', 'hp', 'rs', 'asc', 'udp', 'gx', 'gwm']) {
    await openDev(page, id, 'edit');
    const p2 = page.reqs.length;
    // the Edit modal (the primary too, L3.11b)
    const modal = await editorModalOpen(page);
    if (modal) { await editorSettled(page); await page.click('#devEditSave', { force: true }); }
    await page.waitForTimeout(600);
    const fbText = modal ? await page.locator('#devEditSummary').innerText() : 'no Edit device modal';
    const sent = savesOf(p2, id).length > 0;
    const first = await page.evaluate(async did => {
      const s = window.app._devDetail;
      const r = await fetch(`/api/devices/${encodeURIComponent(did)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(window.app._ddvPayload(did, s.data)) });
      return r.json();
    }, id);
    check(`unchanged save, ${id}: nothing sent; the form's payload is a no-op on the first save`,
          !sent && /Nothing to save|No changes/.test(fbText) && first.status === 'unchanged',
          `${fbText} / ${JSON.stringify(first).slice(0, 90)}`);
  }

  // ── an enabled device that is not running: a Save with nothing changed
  // sends nothing; only Reconnect (the header's, ⋯'s) sends the device
  // (the PUT is answered here: nothing changes) ──
  await openDev(page, 'rs', 'edit');
  let reconnectPut = 0;
  await page.route(/\/api\/devices\/rs$/, r => {
    if (!['PUT', 'PATCH'].includes(r.request().method())) return r.continue();
    reconnectPut++;
    return r.fulfill({ json: { status: 'updated', device: {} } });
  });
  await editorSettled(page);
  await page.click('#devEditSave', { force: true });
  await page.waitForTimeout(500);
  check('not running: the modal\'s Save with nothing changed sends nothing', reconnectPut === 0,
        await page.locator('#devEditSummary').innerText());
  await closeDeviceEditor(page);
  await page.evaluate(() => { window.app._devDetail.running = false; return window.app.ddvReconnect('rs'); });
  await page.waitForTimeout(800);
  check('Reconnect sends the device again', reconnectPut === 1, String(reconnectPut));
  await page.unroute(/\/api\/devices\/rs$/);

  // ── an interval-only change on the primary meter (L3.11b: in the Edit
  // modal): one PATCH with the intervals alone — no connection, so no
  // reconnect; no POST of its own to /poll-groups ──
  await openDev(page, 'umg512', 'edit');
  if (await editorModalOpen(page)) {
    await editorSection(page, 'polling');
    await page.waitForSelector('#devEditModal .ddv-pg');
    const pgId = await page.locator('#devEditModal .ddv-pg').first().getAttribute('id');
    const was = await page.inputValue(`#${pgId}`);
    await page.fill(`#${pgId}`, String(Number(was) + 1));
    const q0 = page.reqs.length;
    const r = await saveDeviceEditor(page);
    const sentP = page.reqs.slice(q0).filter(x => x.method !== 'GET' && !/dry_run/.test(x.url));
    const body = JSON.parse((sentP.find(x => x.method === 'PATCH') || {}).body || '{}');
    check('primary: an interval-only change is one PATCH of the intervals alone (no connection: no reconnect), no POST to /poll-groups',
          r && r.ok() && sentP.length === 1 && body.sections && body.sections.polling && !body.sections.connection
          && !sentP.some(x => /\/poll-groups$/.test(x.url)),
          sentP.map(x => `${x.method} ${x.url.replace(BASE, '')} ${x.body || ''}`).join(', '));
    await openDev(page, 'umg512', 'edit');
    await editorSection(page, 'polling');
    await page.fill(`#${pgId}`, was);
    await saveDeviceEditor(page);
  } else check('primary: the Edit device modal opens', false);

  // ── a component's page (D17) ──
  await openDev(page, 'gx-battery-288');
  // the parent is reached once: the breadcrumb, and one "Open device" in the ⋯ menu
  const inBody = await page.locator('#deviceDetailView button:visible:has-text("Open device")').count();
  const inMenu = await page.locator('#ddvActionsMenu [role=menuitem]:has-text("Open device")').count();
  check('component: one "Open device"', inBody === 0 && inMenu === 1, `${inBody}+${inMenu}`);
  check('component: its type by name', /Battery 288/.test(await page.locator('#deviceDetailView [data-component-of]').innerText()));
  // ⋯ → Rename… (L3.10: the component's actions are menu items, from any
  // surface) — the Edit device modal at its Identity (L3.11b)
  const renameInModal = async name => {
    await page.waitForSelector('#devEditModal.active #ddvName');
    await page.fill('#ddvName', name);
    return saveDeviceEditor(page);
  };
  await deviceAction(page, '[data-ddv-act="rename"]');
  const rn = await renameInModal('Shunt bank');
  await page.waitForTimeout(800);
  check('component: Rename in the Edit device modal (its device\'s route), the page shows the new name',
        page.dlg.native === 0 && rn && rn.ok() && /\/api\/devices\/gx\/components\/gx-battery-288$/.test(rn.url())
        && /Shunt bank/.test(await page.locator('#deviceDetailView h2').innerText()));

  // ── a component from the list's ⋯, no page open (L3.10 review B1) ──
  const compRow = async () => (await api('/api/devices')).devices.find(d => d.id === 'gx-battery-288') || {};
  const rowItem = async item => {
    await page.evaluate(() => window.app.closeDeviceDetail(true));
    await page.waitForSelector('#devicesList [data-dev-menu="gx-battery-288"]');
    await page.click('#devicesList [data-dev-menu="gx-battery-288"]');
    await page.waitForSelector('#devMenu-list-gx-battery-288:not([hidden])');
    await page.click(`#devMenu-list-gx-battery-288 [data-ddv-act="${item}"]`);
    await page.waitForTimeout(1500);
  };
  await rowItem('rename');
  await renameInModal('Shunt bank B');
  check('list ⋯: Rename… of a component (no page open): its page, the modal; renamed', (await compRow()).name === 'Shunt bank B', (await compRow()).name);
  // the fixture's component is under a parent that is off: its Enable is
  // held, saying so (L3.10-F9); with its parent on, Disable… then Enable
  // from the row, then its parent off again, as it was
  check('list ⋯: a component under a device that is off — Enable held, its device named', await (async () => {
    await page.evaluate(() => window.app.closeDeviceDetail(true));
    await page.click('#devicesList [data-dev-menu="gx-battery-288"]');
    await page.waitForSelector('#devMenu-list-gx-battery-288:not([hidden])');
    const it = page.locator('#devMenu-list-gx-battery-288 [data-ddv-act="enable"]');
    const ok = (await it.getAttribute('aria-disabled')) === 'true' && /is off/.test(await it.innerText());
    await page.keyboard.press('Escape');
    return ok;
  })());
  const putGx = on => fetch(BASE + '/api/devices/gx', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: on }) });
  await putGx(true);
  await page.evaluate(() => window.app.renderDevicesList());
  await rowItem('disable');
  check('list ⋯: Disable… of a component disables it (no page open)', (await compRow()).enabled === false);
  check('list ⋯: …and the list is drawn again (its ⋯ now offers Enable)', await (async () => {
    await page.click('#devicesList [data-dev-menu="gx-battery-288"]');
    await page.waitForSelector('#devMenu-list-gx-battery-288:not([hidden])');
    const has = await page.locator('#devMenu-list-gx-battery-288 [data-ddv-act="enable"]:not([aria-disabled="true"])').count() === 1;
    await page.keyboard.press('Escape');
    return has;
  })());
  await rowItem('enable');
  check('list ⋯: Enable of a component enables it', (await compRow()).enabled === true);
  await putGx(false);
  await rowItem('rename');
  await renameInModal('Shunt bank');

  // ── deleting a parent names its components (D18) ──
  await openDev(page, 'gx');
  page.dlg.answers.push(false);
  await deviceAction(page, '[data-ddv-act="delete"]');
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
  await deviceAction(page, '[data-ddv-act="delete"]');
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
    check(`${role}: no component Rename / Remove (in its ⋯ or anywhere)`, !(await rp.locator('#ddvActionsMenu [data-ddv-act="rename"], #ddvActionsMenu [data-ddv-act="delete"], #deviceDetailView [data-action="renameComponentUi"]:visible, #deviceDetailView [data-action="removeComponentUi"]:visible').count()));
    await rp.evaluate(() => window.app.openDeviceDetail('rs'));
    await rp.waitForTimeout(600);
    check(`${role}: no Save & Apply, no Delete`, !(await rp.locator('#deviceDetailView [data-action="saveDeviceDetail"]:visible, #deviceDetailView [data-action="deleteDevice"]:visible').count()));
    check(`no page errors (${role})`, rp.errs.length === 0, rp.errs.join(' | '));
    await rp.close();
  }

  // ── on a phone: bridge fields full width (D20) ──
  const phone = await open({ width: 390, height: 844 });
  await phone.evaluate(() => window.app.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: 'ws', bridge_port: 15996, unit_id: 4 }));
  await phone.waitForSelector('#csAddBus');
  const w = await phone.evaluate(() => document.getElementById('csAddBus').getBoundingClientRect().width);
  check('phone: the bridge picker is full width', w >= 300, String(w));
  if (OUT) await phone.screenshot({ path: `${OUT}/devices-phone-bridge.png` });
  await phone.close();

  // ── Remove… of a component from the table view's ⋯, no page open (L3.10 review B1) ──
  const tp = await open();
  await tp.evaluate(() => { location.hash = '#/devices?view=list'; });
  await tp.waitForSelector('.find-table [data-dev-menu="gx-battery-288"]');
  await tp.click('.find-table [data-dev-menu="gx-battery-288"]');
  await tp.waitForSelector('#devMenu-table-gx-battery-288:not([hidden])');
  await tp.waitForFunction(() => !document.querySelector('#devMenu-table-gx-battery-288 .is-checking'));
  tp.dlg.answers.push(true);
  await tp.click('#devMenu-table-gx-battery-288 [data-ddv-act="delete"]');
  await tp.waitForTimeout(2000);
  const asked = tp.dlg.seen[tp.dlg.seen.length - 1] || {};
  check('table ⋯: Remove… of a component asks first, then removes it (no page open)',
        /Remove component/.test(asked.title || '') && !(await api('/api/devices')).devices.some(d => d.id === 'gx-battery-288'),
        JSON.stringify(asked).slice(0, 200));
  check('no page errors (table)', tp.errs.length === 0, tp.errs.join(' | '));
  await tp.close();
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
