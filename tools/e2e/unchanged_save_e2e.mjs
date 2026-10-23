// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// "An unchanged save changes nothing", through the real editors. Against a
// throwaway gateway whose config.yaml has tools/e2e/fixtures/unchanged_save/
// config-extra.yaml merged in and its rules.yaml (fake addresses only; devices
// disabled, rules in shadow but one armed on a unit that does not exist on the
// wire) — devices on a bridge, a
// serial line, ASCII, UDP, an MQTT GX with a component, an installation whose
// source carries tuning, a tap bank, five rules of every kind). Every editor
// is opened and saved as it is: the server must answer "unchanged" (it runs
// the stored object through the same pipeline), and the stored rule must be
// the same. Then the guards: clearing a field clears it, an armed rule asks
// before it changes or leaves armed, a new template never replaces one.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8097 node tools/e2e/unchanged_save_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { inflateRawSync } from 'node:zlib';
import yaml from 'js-yaml';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8097';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();
const rules = async () => Object.fromEntries((await api('/api/rules')).rules.map(r => [r.id, r]));

// what is stored: config.yaml out of the export archive (secrets masked the
// same way every time, so a comparison still holds)
async function stored() {
  const buf = Buffer.from(await (await fetch(BASE + '/api/config/export')).arrayBuffer());
  for (let i = 0; i + 30 < buf.length;) {
    if (buf.readUInt32LE(i) !== 0x04034b50) break;
    const method = buf.readUInt16LE(i + 8), size = buf.readUInt32LE(i + 18);
    const nlen = buf.readUInt16LE(i + 26), xlen = buf.readUInt16LE(i + 28);
    const name = buf.toString('utf8', i + 30, i + 30 + nlen);
    const data = buf.subarray(i + 30 + nlen + xlen, i + 30 + nlen + xlen + size);
    if (name === 'config.yaml') return yaml.load((method === 8 ? inflateRawSync(data) : data).toString('utf8'));
    i += 30 + nlen + xlen + size;
  }
  throw new Error('config.yaml not in the export');
}
const devRaw = (c, id) => (c.devices || []).find(d => d.id === id);
const epRaw = (c, id) => (c.endpoints || []).find(e => e.id === id);
// every value `a` holds is still in `b` (b may say more: an explicit default)
function lost(a, b, path = '') {
  if (a && typeof a === 'object' && !Array.isArray(a)) {
    return Object.keys(a).flatMap(k => (b && typeof b === 'object' && k in b) ? lost(a[k], b[k], `${path}.${k}`) : [`${path}.${k}`]);
  }
  if (Array.isArray(a)) {
    if (!Array.isArray(b) || b.length !== a.length) return [path];
    return a.flatMap((x, i) => lost(x, b[i], `${path}[${i}]`));
  }
  return a === b || (typeof a === 'number' && Number(a) === Number(b)) ? [] : [`${path} (${JSON.stringify(a)} → ${JSON.stringify(b)})`];
}

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push(m.text()); });
const dlg = await watchDialogs(page);
const said = () => dlg.seen.map(d => [d.title, d.message, ...(d.details || [])].join(' '));

// the next PUT/POST to a path, and what the server answered
const nextWrite = (re) => page.waitForResponse(r => re.test(r.url()) && ['PUT', 'POST'].includes(r.request().method()), { timeout: 8000 })
  .then(async r => ({ status: r.status(), body: await r.json().catch(() => ({})), sent: r.request().postDataJSON() }))
  .catch(() => null);

try {
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});

  // ── devices: an untouched page sends nothing; its own payload, sent
  // anyway, loses nothing and is a no-op from the first save (PROD-1/2) ──
  const saveDevice = async (id) => {
    await page.evaluate(i => window.app.openDeviceDetail(i), id);
    await openDeviceTab(page, 'connection');
    const w = nextWrite(new RegExp(`/api/devices/${id}$`));
    await page.click('[data-action="saveDeviceDetail"]');
    const clicked = await w;                       // null: nothing was sent
    const r = await page.evaluate(async did => {
      const res = await fetch(`/api/devices/${encodeURIComponent(did)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(window.app._ddvPayload(did, window.app._devDetail.data)) });
      return { status: res.status, body: await res.json().catch(() => ({})) };
    }, id);
    return { clicked, ...r };
  };
  for (const id of ['hp', 'rs', 'asc', 'udp', 'gx', 'gwm', 'umg512']) {   // web: the sandbox refuses its loopback URL on any save
    const before = devRaw(await stored(), id);
    const r1 = await saveDevice(id);
    const gone = lost(before, devRaw(await stored(), id));
    check(`device ${id}: an untouched Save sends nothing; its payload keeps everything stored`,
          r1.clicked === null && r1.status === 200 && !gone.length && r1.body.status === 'unchanged',
          `${r1.clicked ? 'a request was sent' : ''} ${r1.status} lost: ${gone.join(', ')} · ${JSON.stringify(r1.body).slice(0, 160)}`);
    const r2 = await saveDevice(id);
    check(`device ${id}: saved again unchanged, it is a no-op`, r2.clicked === null && r2.body.status === 'unchanged',
          `${r2.status} ${JSON.stringify(r2.body).slice(0, 160)}`);
    if (OUT && id === 'hp') await page.screenshot({ path: `${OUT}/us-hp-edit.png` });
  }
  await page.evaluate(() => window.app.openDeviceDetail('hp'));
  await openDeviceTab(page, 'connection');
  check('device on a bridge: the editor names the bridge, not an address',
        (await page.inputValue('#ddvBridge')) === 'ws' && (await page.locator('#ddvHost, #ddvRtuHost').count()) === 0);
  await page.evaluate(() => window.app.openDeviceDetail('rs'));
  await openDeviceTab(page, 'connection');
  check('serial device: the whole line is shown', (await page.inputValue('#ddvBaud')) === '19200'
        && (await page.inputValue('#ddvParity')) === 'E' && (await page.inputValue('#ddvStop')) === '2');
  await page.evaluate(() => window.app.openDeviceDetail('asc'));
  await openDeviceTab(page, 'connection');
  check('ASCII device: its protocol is the one shown',
        await page.isChecked('input[name="ddvProto"][value="rtu"]') && await page.isChecked('input[name="ddvRtuMode"][value="rtu"]')
        && (await page.inputValue('#ddvFraming')) === 'ascii');

  // ── installations ──
  const twice = async (label, epId, open) => {
    const before = epRaw(await stored(), epId);
    for (const n of [1, 2]) {
      await open();
      const w = nextWrite(new RegExp(`/api/endpoints/${epId}$`));
      await page.click('#endpointModal [data-endpoint-save]');
      const r = await w;
      if (n === 1) {
        const gone = lost(before, epRaw(await stored(), epId));
        check(`${label}: a save keeps everything stored`, r && r.status === 200 && !gone.length,
              r ? `${r.status} lost: ${gone.join(', ')} · ${JSON.stringify(r.body).slice(0, 200)}` : 'no request');
      } else {
        check(`${label}: saved again unchanged, it is a no-op`, r && r.body.status === 'unchanged',
              r ? `${r.status} ${JSON.stringify(r.body).slice(0, 200)}` : 'no request');
      }
    }
  };
  await page.evaluate(() => window.app.openEndpointDetail('pv'));
  await page.waitForTimeout(800);
  await twice('installation source', 'pv', async () => {
    await page.evaluate(() => window.app.openSourceModal('pv', 'sunspec', 'inverters'));
    await page.waitForSelector('#srcProto');
  });
  await page.evaluate(() => window.app.openEndpointModal('pv'));
  await page.waitForSelector('#plName');
  check('Edit installation: HA discovery shows as on (its default)', await page.isChecked('#plHaDisc'));
  await page.evaluate(() => window.app.closeModal('endpointModal'));
  await twice('Edit installation', 'pv', async () => {
    await page.evaluate(() => window.app.openEndpointModal('pv'));
    await page.waitForSelector('#plName');
  });
  const pvNow = epRaw(await stored(), 'pv');
  check('Edit installation: the units keep HA discovery', pvNow.mqtt?.ha_discovery !== false, JSON.stringify(pvNow.mqtt));
  check('a group that inherits still inherits', !('connection' in pvNow.groups[1]) && !('sources' in pvNow.groups[1]),
        JSON.stringify(pvNow.groups[1]));

  await page.evaluate(() => window.app.openEndpointDetail('bank'));
  await page.waitForTimeout(800);
  await page.evaluate(() => window.app.openSourceModal('bank', 'default', 'units'));
  await page.waitForSelector('#srcProto');
  check('tap bank: the source editor offers the tap and shows its line',
        (await page.inputValue('#srcProto')) === 'rtu_tap' && (await page.inputValue('#srcBaud')) === '19200');
  await page.evaluate(() => window.app.closeModal('endpointModal'));
  await twice('tap bank', 'bank', async () => {
    await page.evaluate(() => window.app.openSourceModal('bank', 'default', 'units'));
    await page.waitForSelector('#srcProto');
  });
  check('tap bank: still a group connection, never a source', 'connection' in epRaw(await stored(), 'bank').groups[0]);
  // what the modal shows for it is saved on the connection (how often it reads)
  await page.evaluate(() => window.app.openSourceModal('bank', 'default', 'units'));
  await page.waitForSelector('#srcIvNormal');
  check('tap bank: no template choice for a group connection', await page.locator('#srcTpl').isHidden());
  await page.fill('#srcIvNormal', '7');
  let wb = nextWrite(/\/api\/endpoints\/bank$/);
  await page.click('#endpointModal [data-endpoint-save]');
  await wb;
  const bc = epRaw(await stored(), 'bank').groups[0].connection;
  check('tap bank: an interval edited there is saved', bc?.poll_groups?.normal?.interval === 7 && bc.baudrate === 19200,
        JSON.stringify(bc));

  // ── rules ──
  await page.evaluate(() => window.app.navigateTo('rules'));
  await page.waitForTimeout(800);
  const before = await rules();
  for (const id of Object.keys(before)) {
    dlg.seen.length = 0;
    await page.evaluate(i => window.app.openRuleModal(i), id);
    await page.waitForSelector('#rlKind');
    await page.click('#ruleModal [data-rule-save]');
    await page.waitForTimeout(600);
    const after = (await rules())[id];
    check(`rule ${id}: an unchanged save keeps it as it was`,
          JSON.stringify(after.raw) === JSON.stringify(before[id].raw),
          `${JSON.stringify(before[id].raw)} → ${JSON.stringify(after.raw)} · ${await page.locator('#ruleFeedback').innerText().catch(() => '')}`);
  }
  // a real change to an armed rule asks first, and a "no" sends nothing
  dlg.answer = false;
  dlg.seen.length = 0;
  await page.evaluate(() => window.app.openRuleModal('ov-u1'));
  await page.waitForSelector('#rlLabel');
  await page.fill('#rlLabel', 'Over-voltage · changed');
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(400);
  check('rule: changing an armed rule asks first, "no" keeps it',
        said().some(m => /armed/i.test(m)) && (await rules())['ov-u1'].raw.label === before['ov-u1'].raw.label, said().join(' | '));
  dlg.answer = true;
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  // clearing a field clears it
  await page.evaluate(() => window.app.openRuleModal('cond'));
  await page.waitForSelector('#rlUntil');
  await page.fill('#rlUntil', '');
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(600);
  check('rule: clearing "Until" clears it', !('release' in (await rules()).cond.raw));
  await page.evaluate(() => window.app.openRuleModal('ov-yield'));
  await page.waitForSelector('#rlPriority');
  await page.fill('#rlPriority', '');
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(600);
  const oy = (await rules())['ov-yield'].raw;
  check('rule: clearing the priority clears it, "no opinion" stays', !('priority' in oy) && oy.normal === null, JSON.stringify(oy));
  // an emptied value is an error, never a silent "no opinion"
  await page.evaluate(() => window.app.openRuleModal('ov-u1'));
  await page.waitForSelector('#rlNormal');
  await page.fill('#rlNormal', '');
  dlg.seen.length = 0;
  await page.click('#ruleModal [data-rule-save]');
  await page.waitForTimeout(400);
  check('rule: an emptied Normal is refused in the form',
        /No opinion|Fără opinie/.test(await page.locator('#ruleFeedback').innerText()) && (await rules())['ov-u1'].raw.normal?.value === 100);
  await page.evaluate(() => window.app.closeModal('ruleModal'));
  // leaving armed asks, names the safe values, and a "no" sends nothing
  dlg.answer = false;
  dlg.seen.length = 0;
  await page.evaluate(() => window.app.setRuleMode('ov-u1', 'shadow'));
  await page.waitForTimeout(400);
  check('rule: leaving armed asks first and names the safe values', said().some(m => /value=100/.test(m)), said().join(' | '));
  check('rule: a "no" keeps it armed', (await rules())['ov-u1'].mode === 'armed');
  dlg.answer = true;

  // ── templates ──
  const tpl = { device_template: { id: 'e2e-own', name: 'E2E own', protocol: { transports: ['tcp'] },
                registers: [{ address: 1, name: 'v', data_type: 'uint16' }] } };
  const dropTpls = () => Promise.all(['e2e-own', 'e2e-own-copy', 'e2e-own-copy-2'].map(id =>
    fetch(`${BASE}/api/device-templates/${id}`, { method: 'DELETE' })));
  await dropTpls();
  await fetch(BASE + '/api/device-templates', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(tpl) });
  await page.evaluate(() => window.app.navigateTo('templates'));
  await page.waitForTimeout(500);
  await page.evaluate(() => window.app.openTplEditor('e2e-own', true));
  await page.waitForSelector('#devTplSaveBtn');
  const dupId = await page.evaluate(() => window.app._tplEdit.data.id);
  check('template: a duplicate gets a free id', dupId === 'e2e-own-copy', dupId);
  await page.fill('#tplId', 'e2e-own');
  await page.fill('#tplName', 'REPLACED');
  dlg.answer = false;
  dlg.seen.length = 0;
  await page.click('#devTplSaveBtn');
  await page.waitForTimeout(600);
  const kept = await api('/api/device-templates/e2e-own');
  check('template: a new map on a taken id asks, and "no" keeps the old one',
        said().some(m => /e2e-own/.test(m)) && kept.device_template.name === 'E2E own', said().join(' | '));
  dlg.answer = true;
  await page.evaluate(() => window.app.closeModal('devTplModal'));
  await dropTpls();

  check('no page errors', errs.length === 0, errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
