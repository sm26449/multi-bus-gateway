// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The Status page against a fixture plant of
// 25 devices in every state, served by mocking the routes the page reads.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 node tools/e2e/status_page_e2e.mjs
import { chromium } from 'playwright';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};

// ── the fixture plant ──
const dev = (id, o = {}) => ({ id, name: o.name || id, enabled: true, protocol: 'tcp', health: 'ok', data_health: 'ok',
  connected: true, poll_rate: 0.5, staleness_age_s: 1, last_latency_ms: 40, failed_reads: 0, error_counts: {}, up_since_s: 7200, endpoint_id: '', ...o });
const devices = [
  dev('umg512', { name: 'Janitza UMG 512', primary: true, poll_rate: 4.1, last_latency_ms: 18 }),
  ...[1, 2, 3, 4].map(u => dev(`pv-u${u}`, { name: `Inverter ${u}`, endpoint_id: 'pv', read_via: [{ id: 's', protocol: 'http', timeout_s: 3 }] })),
  dev('seplos-p1', { endpoint_id: 'bank', protocol: 'mqtt', poll_rate: null }), dev('seplos-p2', { endpoint_id: 'bank', protocol: 'mqtt', poll_rate: null }),
  dev('samsung-dvm', { name: 'Heat pump', health: 'down', data_health: 'down', failed_reads: 12, error_counts: { timeout: 12 }, up_since_s: 240, connected: false }),
  dev('vfd', { name: 'Delta VFD', health: 'degraded', data_health: 'degraded', up_since_s: 50 }),
  dev('h1', { name: 'Bridge meter 1', health: 'down', connected: false }), dev('h2', { name: 'Bridge meter 2', health: 'down', connected: false }),
  dev('victron-gx', { protocol: 'mqtt', poll_rate: null }),
  dev('old-meter', { enabled: false, health: 'idle', data_health: 'idle' }),
  ...Array.from({ length: 12 }, (_, i) => dev(`sensor-${String(i + 1).padStart(2, '0')}`, { protocol: 'rtu', poll_rate: 0.2 })),
];
const fx = {
  role: 'active', fail: {},
  status: () => ({
    version: '3.92.0', websocket_clients: 2, last_update: Date.now() / 1000,
    config: { healthy: true },
    modbus: { poll_groups_detail: [{ name: 'realtime', interval: 1, age_s: 0.4, poll_count: 9000 }] },
    mqtt: { enabled: true, connected: true, broker: '192.0.2.10', port: 1883, messages_published: 100000 + Math.round(Date.now() / 100) % 100000, messages_skipped: 50, messages_failed: 0, connection_count: 1, last_contact_age_s: 0.5 },
    influxdb: { enabled: true, connected: true, bucket: 'mbg', writes_total: 5000, writes_confirmed: 4990, last_confirm_age_s: 2, buffer_points: 0, buffer_minutes: 60, dropped_total: 0, writes_failed: 0, replayed_total: 3 },
    devices: fx.devicesNow || devices,
    endpoints: [
      { id: 'pv', name: 'PV plant', enabled: true, status: 'online', units_online: 4, units_total: 4, units_asleep: 0, aggregates_enabled: true },
      { id: 'bank', name: 'Battery bank', enabled: true, status: 'online', units_online: 2, units_total: 2, aggregates_enabled: true },
    ],
  }),
  health: () => ({ status: 'ok', role: { role: fx.role, epoch: 1, since: Date.now() / 1000 - 86400, set_by: 'boot', reason: '' }, meters: [] }),
  vm: () => ({ instances: [{ name: 'Grid meter', template: 'sdm630', port: 502, enabled: true, running: true, state: 'ok', conn_count: 1, connections: [{ ip: '192.0.2.20', port: 40001 }], req_rate: 2, requests: 900, errors: 0, bytes_rx: 2048, bytes_tx: 4096, freshness_age_s: 0.5 }] }),
  res: () => ({ cpu_pct: 3.4, rss_mb: 142, threads: 31, open_fds: 60, tcp_established: 14, uptime_s: 3 * 86400, num_cpus: 4 }),
  rules: () => ({ rules: [
    { id: 'ov-u1', label: 'Over-voltage U1', enabled: true, mode: 'armed', kind: 'steps', target: { device: 'pv-u1', command: 'power_limit' }, live: { state: 'normal', units: { 'pv-u1': {} }, last: { ts: Date.now() / 1000 - 30, action: 'hold' } } },
    { id: 'export-cap', label: 'Export cap', enabled: true, mode: 'shadow', kind: 'trim', target: { device: 'pv-u1', command: 'power_limit' }, live: { state: 'holding', units: {} } },
    { id: 'pump', label: 'Pump on sun', enabled: true, mode: 'armed', kind: 'condition', target: { device: 'vfd', command: 'run' }, live: { state: 'unbound', error: 'unknown name "x"', units: {} } },
    { id: 'old', label: 'Old rule', enabled: false, mode: 'armed', kind: 'condition', target: { device: 'vfd', command: 'run' }, live: { state: 'lowering', units: {} } },
  ] }),
  sequences: () => ({ sequences: [{ id: 'restart', label: 'Restart inverters', steps: [], running: null, last: [{ status: 'done', started: Date.now() / 1000 - 4000 }] }] }),
  bridges: () => ({ bridges: [{ id: 'br1', name: 'Heat bridge', type: 'waveshare', type_name: 'Waveshare', host: '192.0.2.30', devices: 2, state: { status: 'offline' },
                               ports: [{ port: 1, label: 'RS485-1', devices: [{ id: 'h1' }, { id: 'h2' }], busy: { pct: 12, level: 'ok' } }] }] }),
  opcua: () => ({ enabled: false, running: false }),
  bus: () => ({ role: 'active', listened: [], polled: [{ bus: '/dev/ttyUSB0', devices: ['sensor-01'], answering: true }] }),
  alerts: () => ({ status: { enabled: true, channels: ['mqtt'], thresholds: { latency_ms: 1500 } }, alerts: [{ ts: Date.now() / 1000 - 60, severity: 'error', source: 'Heat pump', message: 'down' }] }),
  extraEvents: [],
  events: () => ({ events: [
    ...fx.extraEvents,
    { ts: T0 - 10, level: 'warning', source: 'Delta VFD', message: 'degraded', kind: 'transition' },
    { ts: T0 - 70, level: 'info', source: 'gateway', message: 'started', kind: '' },
    { ts: T0 - 90, level: 'critical', source: 'Heat pump', message: 'down', kind: 'transition' },
    ...Array.from({ length: 57 }, (_, i) => ({ ts: T0 - 600 - i * 3600, level: i % 3 ? 'info' : 'warn',
      source: `sensor-${String(i % 5 + 1).padStart(2, '0')}`, message: i % 3 ? `poll ok ${i}` : `slow answer ${i}`, kind: '' }))] }),
};
const ROUTES = [['/api/status/resources', 'res'], ['/api/status', 'status'], ['/health', 'health'], ['/api/virtual-meters', 'vm'],
  ['/api/rules', 'rules'], ['/api/sequences', 'sequences'], ['/api/bridges', 'bridges'], ['/api/opcua', 'opcua'],
  ['/api/role/bus', 'bus'], ['/api/alerts', 'alerts'], ['/api/events', 'events']];
let statusHits = 0;
const T0 = Date.now() / 1000;      // event times are fixed: a log does not rewrite its past
const routeHits = {};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(width, height) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.setDefaultTimeout(15000);
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push(m.text()); });
  await page.route(u => ROUTES.some(([p]) => new URL(u).pathname === p), async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET') return route.continue();
    const key = ROUTES.find(([p]) => p === path)[1];
    if (key === 'status') statusHits++;
    routeHits[key] = (routeHits[key] || 0) + 1;
    if (fx.fail[key]) return route.fulfill({ status: 500, body: 'boom' });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fx[key]()) });
  });
  await page.addInitScript(() => { try { localStorage.removeItem('mbg-status-tab'); localStorage.removeItem('mbg-status-devfilter'); } catch (e) {} });
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return { page, errs };
}
const shot = async (page, name) => { if (OUT) await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false }); };
const noHScroll = page => page.evaluate(() => {
  const host = document.getElementById('statusPage');
  return document.documentElement.scrollWidth <= window.innerWidth + 1 && (!host || host.scrollWidth <= host.clientWidth + 1);
});

try {
  const { page, errs } = await open(1440, 950);
  await page.click('[data-page="status"]');
  await page.waitForSelector('#stAttention .st-issue');
  await page.waitForTimeout(2500);
  await shot(page, 'st-desk-devices');

  // the attention list: errors first, causes folded, Open buttons named
  const issues = await page.$$eval('#stAttention .st-issue', els => els.map(e => ({ key: e.dataset.key, cls: e.className, open: e.querySelector('button')?.getAttribute('aria-label') })));
  const keys = issues.map(i => i.key);
  check('attention: errors before warnings', issues.findIndex(i => /st-warn/.test(i.cls)) > issues.findLastIndex(i => /st-error/.test(i.cls)), keys.join());
  check('attention: the bridge folds its devices', keys.includes('bridge:br1') && !keys.includes('dev:h1') && !keys.includes('dev:h2'), keys.join());
  check('attention: device down and rule error listed', keys.includes('dev:samsung-dvm') && keys.includes('rule:pump'), keys.join());
  check('attention: degraded device is a warning', issues.some(i => i.key === 'dev:vfd' && /st-warn/.test(i.cls)));
  check('attention: Open buttons have distinct names', new Set(issues.map(i => i.open)).size === issues.length, issues.map(i => i.open).join('|'));
  check('attention: disabled things are no problem', !keys.includes('dev:old-meter') && !keys.includes('rule:old'));

  // pills
  const pillDev = await page.locator('#stPills [data-key="devices"] .st-pill-value').innerText();
  check('pill: devices online of enabled (disabled left out)', pillDev === '21/24', pillDev);
  const titlebar = await page.locator('#devicesOnlineCount').innerText().catch(() => '');
  check('titlebar count agrees with the pill', titlebar === pillDev, `${titlebar} vs ${pillDev}`);
  check('pill: automation counts armed and shadow', /2/.test(await page.locator('#stPills [data-key="automation"]').innerText()));

  // devices tab: worst first, filters, search
  const firstRow = await page.locator('#stDevices tbody tr').first().getAttribute('data-key');
  check('devices: worst first', ['samsung-dvm', 'h1', 'h2'].includes(firstRow), firstRow);
  check('devices: poll groups under the primary', await page.locator('#stDevices tr[data-key="umg512:groups"]').count() === 1);
  check('devices: disabled hidden from All', await page.locator('#stDevices tr[data-key="old-meter"]').count() === 0);
  await page.click('#stDevFilter [data-v="problems"]');
  await page.waitForTimeout(200);
  const probRows = await page.$$eval('#stDevices tbody tr:not(.st-sub)', r => r.map(x => x.dataset.key));
  check('devices: Problems filter', probRows.length === 4 && probRows.every(k => ['samsung-dvm', 'h1', 'h2', 'vfd'].includes(k)), probRows.join());
  await page.click('#stDevFilter [data-v="disabled"]');
  await page.waitForTimeout(200);
  check('devices: Disabled filter', (await page.$$eval('#stDevices tbody tr', r => r.map(x => x.dataset.key))).join() === 'old-meter');
  await page.click('#stDevFilter [data-v="all"]');
  await page.fill('#stDevSearch', 'sensor-1');
  await page.waitForTimeout(200);
  check('devices: search', (await page.locator('#stDevices tbody tr').count()) === 3);

  // a refresh keeps the search box's text and focus, and a focused row
  await page.focus('#stDevSearch');
  await page.waitForTimeout(4500);
  check('refresh keeps the search focus and text', await page.evaluate(() => document.activeElement?.id === 'stDevSearch' && document.activeElement.value === 'sensor-1'));
  await page.fill('#stDevSearch', '');
  await page.waitForTimeout(200);
  await page.focus('#stDevices tr[data-key="umg512"]');
  await page.evaluate(() => { document.activeElement._stMark = 1; });
  await page.waitForTimeout(4500);
  check('refresh keeps a focused row (the same element)', await page.evaluate(() => document.activeElement?._stMark === 1 && document.activeElement.dataset.key === 'umg512'));

  // tabs: keyboard, memory
  await page.focus('#stTab-devices');
  await page.keyboard.press('ArrowRight');
  check('tabs: arrow key moves', await page.evaluate(() => document.activeElement?.id === 'stTab-installations' && !document.getElementById('stPanel-installations').hidden));
  check('installations drawn', (await page.locator('#stInstallations .st-inst').count()) === 2);
  await page.click('#stPills [data-key="outputs"]');
  await page.waitForTimeout(300);
  await shot(page, 'st-desk-outputs');
  check('outputs: OPC UA off reads off with a link', /Not enabled|Neactivat/.test(await page.locator('#stOutputs [data-key="opcua"]').innerText()));
  check('outputs: alert Test button shown for an admin', await page.locator('#statusTestAlertBtn').isVisible());
  check('alerts: card spaced like the others', await page.evaluate(() => parseFloat(getComputedStyle(document.getElementById('stAlertsBox')).marginTop) >= 8));
  check('alerts: the signals are listed', (await page.locator('#stAlertBody .st-sigs .st-badge').count()) === 6);
  check('routing: one row per enabled device', (await page.locator('#stRoutingBox tbody tr').count()) === 24);
  await page.click('#stRoutingBox summary');
  await page.waitForTimeout(4500);
  check('routing: stays open across a refresh', await page.evaluate(() => document.getElementById('stRoutingBox').open));

  // the Test button keeps its spinner/result across a refresh
  await page.route('**/api/alerts/test', r => setTimeout(() => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ delivered: true, channels: { mqtt: 'sent' } }) }), 3000));
  await page.click('#statusTestAlertBtn');
  await page.waitForTimeout(2500);
  check('Test: spinner survives a refresh', await page.locator('#statusTestAlertBtn .btn-spinner').count() === 1);
  await page.waitForTimeout(1500);
  check('Test: the result is shown', /sent/.test(await page.locator('#alertTestResultStatus').innerText()));
  await page.evaluate(() => document.body.classList.add('role-viewer'));
  check('Test: hidden for a viewer', !(await page.locator('#statusTestAlertBtn').isVisible()));
  await page.evaluate(() => document.body.classList.remove('role-viewer'));
  const alertsFx = fx.alerts;
  fx.alerts = () => ({ ...alertsFx(), status: { enabled: false, channels: [] } });
  await page.click('#stTab-devices'); await page.click('#stTab-outputs');
  await page.waitForTimeout(11000);
  check('Test: hidden with no channel', !(await page.locator('#statusTestAlertBtn').isVisible()));
  fx.alerts = alertsFx;

  // a failing route marks only its part
  fx.fail.rules = true;
  await page.click('#stTab-automation');
  await page.waitForTimeout(11000);
  const auto = await page.locator('#stAutomation').innerText();
  check('failing route: its part says not updated, the rest stays', /not updated since|neactualizat/.test(auto) && /Over-voltage U1/.test(auto), auto.slice(0, 200));
  const before = routeHits.rules || 0;
  await page.waitForTimeout(6000);
  check('failing route: asked at its own pace, not every 2 s', (routeHits.rules || 0) - before <= 1, `${(routeHits.rules || 0) - before} in 6 s`);
  fx.fail.rules = false;
  await page.waitForTimeout(2500);
  await shot(page, 'st-desk-automation');

  for (const tab of ['gateway', 'events']) {
    await page.click('#stTab-' + tab);
    await page.waitForTimeout(400);
    await shot(page, 'st-desk-' + tab);
  }
  check('gateway: role and resources', /ACTIVE/.test(await page.locator('#stGateway').innerText()) && /142 MB/.test(await page.locator('#stGateway').innerText()));
  const evRows = () => page.locator('#stEvents tbody tr').count();
  check('events: all 60 listed', (await evRows()) === 60);
  check('events: the list scrolls inside its box', await page.evaluate(() => { const b = document.getElementById('stEventsBox'); return b.scrollHeight > b.clientHeight + 50; }));
  check('events: level counts', (await page.locator('#stEvN-error').innerText()) === '1' && (await page.locator('#stEvN-warn').innerText()) === '20');
  await page.click('#stEvFilter [data-v="error"]');
  await page.waitForTimeout(200);
  check('events: errors only', (await evRows()) === 1);
  await page.click('#stEvFilter [data-v="all"]');
  await page.fill('#stEvSearch', 'slow answer');
  await page.waitForTimeout(200);
  check('events: search', (await evRows()) === 19, String(await evRows()));
  await page.fill('#stEvSearch', '');
  await page.selectOption('#stEvSource', 'sensor-02');
  await page.waitForTimeout(200);
  check('events: source filter', (await evRows()) === 12 && (await page.locator('#stEvCount').innerText()).startsWith('12'), String(await evRows()));
  await page.selectOption('#stEvSource', '');
  await page.waitForTimeout(200);
  await page.locator('#stEvents tbody .st-linkish', { hasText: 'Heat pump' }).first().click();
  await page.waitForTimeout(200);
  check('events: a click on a source filters on it', (await evRows()) === 1 && await page.locator('#stEvSource').inputValue() === 'Heat pump');
  await page.selectOption('#stEvSource', '');
  await page.waitForTimeout(200);
  check('events: older ones carry their date', /[A-Za-z]{3}|\d+\s\w+/.test(await page.locator('#stEvents tbody tr').last().locator('td').first().innerText()));
  await page.evaluate(() => { document.getElementById('stEventsBox').scrollTop = 600; });
  const firstVisible = await page.evaluate(() => {
    const b = document.getElementById('stEventsBox'), r = b.getBoundingClientRect();
    return [...b.querySelectorAll('tbody tr')].find(tr => tr.getBoundingClientRect().top >= r.top + 30)?.dataset.key; });
  fx.extraEvents = [{ ts: Date.now() / 1000, level: 'error', source: 'new', message: 'fresh one', kind: '' }];
  await page.waitForTimeout(11000);
  const stillVisible = await page.evaluate(k => {
    const b = document.getElementById('stEventsBox'), r = b.getBoundingClientRect();
    const tr = b.querySelector(`tbody tr[data-key="${CSS.escape(k)}"]`);
    return tr && Math.abs(tr.getBoundingClientRect().top - (r.top + 30)) < 60; }, firstVisible);
  check('events: a new event does not move a reader who scrolled down', stillVisible && (await evRows()) === 61,
    JSON.stringify({ firstVisible, stillVisible, rows: await evRows(), top: await page.evaluate(() => document.getElementById('stEventsBox').scrollTop) }));
  fx.extraEvents = [];

  // a rule opened from Status is highlighted, and stays so after the Rules page redraws
  await page.click('#stTab-devices');
  await page.click('#stAttention .st-issue[data-key="rule:pump"] button');
  await page.waitForSelector('[data-rule="pump"].st-highlight');
  check('openRule: highlighted', true);
  await page.waitForTimeout(5600);
  check('openRule: still highlighted after the Rules refresh', await page.locator('[data-rule="pump"].st-highlight').count() === 1);

  // leaving Status keeps the titlebar's own refresh alive
  await page.click('[data-page="dashboard"]');
  const hits = statusHits;
  await page.waitForTimeout(6000);
  check('titlebar keeps refreshing after leaving Status', statusHits > hits, `${hits} → ${statusHits}`);

  // the tab is remembered; a language switch rebuilds the frame on it
  await page.click('[data-page="status"]');
  await page.click('#stTab-gateway');
  await page.evaluate(() => window.app.setLanguage('ro'));
  await page.waitForTimeout(1500);
  check('language switch: frame in Romanian, tab kept', /Dispozitive/.test(await page.locator('#stTab-devices').innerText())
    && await page.locator('#stTab-gateway').getAttribute('aria-selected') === 'true');
  await shot(page, 'st-desk-ro');
  await page.evaluate(() => window.app.setLanguage('en'));

  // standby: no red, an explanation
  fx.role = 'standby';
  await page.click('#stTab-devices');
  await page.waitForTimeout(2600);
  check('standby: no attention list', (await page.locator('#stAttention .st-issue').count()) === 0);
  check('standby: header says so', /Standby/.test(await page.locator('#stHead').innerText()));
  check('standby: the devices pill does not read as an outage', !/0\/24/.test(await page.locator('#stPills [data-key="devices"]').innerText()));
  await shot(page, 'st-desk-standby');
  fx.role = 'active';
  check('no page errors (desktop)', errs.length === 0, errs.join(' | '));
  await page.close();

  // phone
  const ph = await open(390, 844);
  await ph.page.click('.nav-toggle, [data-page="status"]').catch(() => {});
  await ph.page.evaluate(() => window.app.navigateTo('status'));
  await ph.page.waitForSelector('#stAttention .st-issue');
  await ph.page.waitForTimeout(2500);
  await shot(ph.page, 'st-phone');
  let ok = true;
  for (const tab of ['devices', 'installations', 'outputs', 'automation', 'gateway', 'events']) {
    await ph.page.evaluate(t => window.app.statusTab(t), tab);
    await ph.page.waitForTimeout(300);
    if (!(await noHScroll(ph.page))) { ok = false; console.log('  horizontal scroll on', tab); }
  }
  check('phone: no horizontal page scroll on any tab', ok);
  check('no page errors (phone)', ph.errs.length === 0, ph.errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
