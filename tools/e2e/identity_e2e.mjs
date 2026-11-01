// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// How new devices are named (ADR-0026, lot L1), as a user meets it:
// Settings → General's "Gateway identity" card (labels, hints, keyboard,
// a refused id said at its field, ro and en), the default patterns on the
// MQTT and InfluxDB tabs (empty = the scope's default), a device added
// afterwards getting the scoped topic and bucket in Add Device step 3, a
// topic prefix another device holds refused at its field in the wizard, and
// the device page still saving (no 412 from the ETag a GET now carries).
//
// Against a throwaway safety fixture (an older install: identity_scope not
// in its file, so `legacy`). It writes: start it fresh, remove it after.
//
//   tools/e2e/fixture_up.sh safety 18120          (MBG_FX_NAME=mbg-fx-l1)
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18120 OUT=<dir> node tools/e2e/identity_e2e.mjs
//   docker rm -f mbg-fx-l1
import { chromium } from 'playwright';
import { openAdd, pickModel, answer, fill, next, create, step } from './lib/add-device.mjs';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab, saveDeviceEditor } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18120';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const api = async (path, opts) => (await fetch(BASE + path, opts)).json();
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function open(viewport = { width: 1440, height: 950 }) {
  const page = await browser.newPage({ viewport });
  page.setDefaultTimeout(15000);
  page.errs = [];
  page.on('pageerror', e => page.errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errs.push(m.text()); });
  page.codes = [];
  page.on('response', r => { if (r.request().method() !== 'GET') page.codes.push([r.request().method(), new URL(r.url()).pathname, r.status()]); });
  await watchDialogs(page);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return page;
}
const shot = async (page, name) => { if (OUT) await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false }); };
const general = async page => {
  await page.evaluate(() => window.app.openSettingsTab('general'));
  await page.waitForSelector('#cfgGatewayId', { state: 'visible' });
  await page.waitForFunction(() => document.getElementById('cfgIdentityExTopic')?.textContent.trim());
};
const saveIdentity = async page => {
  await page.click('[data-action="saveIdentityConfig"]');
  await page.waitForFunction(() => (document.getElementById('identitySaveFeedback')?.textContent || '').trim());
  return (await page.locator('#identitySaveFeedback').innerText()).trim();
};
const describedBy = async (page, sel) => {
  const ids = ((await page.getAttribute(sel, 'aria-describedby')) || '').split(/\s+/).filter(Boolean);
  const texts = [];
  for (const id of ids) texts.push(await page.locator('#' + id).innerText().catch(() => ''));
  return texts.join(' ');
};

// Add Device as a user takes it (L3.9): the model, its connection, the check
// (nothing answers there: Next goes on without one), onto Name & save
async function wizardToStep3(page, host) {
  await openAdd(page);
  await pickModel(page, 'sdm630', 'eastron_sdm630');
  await answer(page, 'PortKind', 'ethernet');
  await answer(page, 'Reach', 'address').catch(() => {});
  await fill(page, 'Host', host);
  await fill(page, 'Port', '15990');
  await fill(page, 'Unit', '1');
  await next(page);
  await next(page);
  await page.waitForSelector('#adOutPrefix', { state: 'attached' });
}
const nameIt = async (page, name) => { await page.fill('#adName', name); await page.locator('#adName').dispatchEvent('input'); await page.waitForTimeout(600); };

try {
  const before = await api('/api/config/identity');
  check('the fixture is an older install: legacy, no id', before.identity_scope === 'legacy' && before.gateway_id === '',
        JSON.stringify(before));

  const page = await open();
  await general(page);
  await shot(page, 'identity-legacy');

  // ── the card: labelled, hinted, keyboard ──
  check('the id field has its label', (await page.locator('label[for="cfgGatewayId"]').innerText()).trim() === 'Gateway id');
  check('the scope select has its label', /How new devices are named/.test(await page.locator('label[for="cfgIdentityScope"]').innerText()));
  const idHint = await describedBy(page, '#cfgGatewayId');
  check('the id hint: it names devices created afterwards, existing ones keep topic, bucket and HA id',
        /devices created afterwards/.test(idHint) && /Home Assistant id/.test(idHint) && /keeps all three/.test(idHint), idHint);
  const scHint = await describedBy(page, '#cfgIdentityScope');
  check('the scope hint: only devices created afterwards, existing ones keep their HA entities',
        /only devices created afterwards/.test(scHint) && /Home Assistant entities/.test(scHint), scHint);
  check('legacy: the example is the old naming',
        /mbg\/devices\/\{device\}/.test(await page.locator('#cfgIdentityExample').innerText()));
  await page.focus('#cfgGatewayId');
  await page.keyboard.press('Tab');
  check('Tab moves from the id to the scope', await page.evaluate(() => document.activeElement?.id) === 'cfgIdentityScope');
  await page.keyboard.press('ArrowUp');                   // gateway is the first option
  check('the scope is chosen with the keyboard', await page.inputValue('#cfgIdentityScope') === 'gateway');

  // ── refusals said, nothing saved ──
  let fb = await saveIdentity(page);
  check('gateway scope without an id is refused, said', /set one/.test(fb), fb);
  await page.fill('#cfgGatewayId', 'Bad Id!');
  fb = await saveIdentity(page);
  check('a bad id is refused with its rule', /lowercase letters/.test(fb), fb);
  check('nothing was saved', (await api('/api/config/identity')).identity_scope === 'legacy');

  // ── saved ──
  await page.fill('#cfgGatewayId', 'site-e2e');
  fb = await saveIdentity(page);
  const after = await api('/api/config/identity');
  check('saved: gateway scope, id site-e2e', /Saved/.test(fb) && after.identity_scope === 'gateway' && after.gateway_id === 'site-e2e',
        fb + ' ' + JSON.stringify(after));
  check('the example follows', /mbg\/site-e2e\/devices\/\{device\}/.test(await page.locator('#cfgIdentityExample').innerText())
        && /site-e2e-\{device\}/.test(await page.locator('#cfgIdentityExample').innerText()));
  await shot(page, 'identity-gateway');

  // ── the default patterns: empty = the scope's ──
  // the identity save read the MQTT and InfluxDB tabs again: no stale form (review M2)
  await page.evaluate(() => window.app.openSettingsTab('mqtt'));
  await page.waitForSelector('#cfgMqttDefaultTopic', { state: 'visible' });
  await page.waitForFunction(() => document.getElementById('cfgMqttDefaultTopic').placeholder.includes('{gateway}'));
  check('MQTT: no pattern chosen → empty, the scope default as placeholder',
        await page.inputValue('#cfgMqttDefaultTopic') === ''
        && await page.getAttribute('#cfgMqttDefaultTopic', 'placeholder') === 'mbg/{gateway}/devices/{device}');
  check('MQTT: the hint says what empty means and names {gateway}',
        /Empty: the default/.test(await page.locator('[data-i18n="mqtt.defaultTopicHint"]').innerText())
        && /\{gateway\}/.test(await page.locator('[data-i18n="mqtt.defaultTopicHint"]').innerText()));
  check('InfluxDB: empty, the scope default as placeholder',
        await page.inputValue('#cfgInfluxDefaultBucket') === ''
        && await page.getAttribute('#cfgInfluxDefaultBucket', 'placeholder') === '{gateway}-{device}');
  // the body the MQTT tab's Save sends: '' (the scope's), never the legacy pattern
  const sent = await page.evaluate(() => window.app._gatherSettings('mqtt')[1].default_topic_pattern);
  check("the MQTT tab's Save sends '' for an empty pattern", sent === '', JSON.stringify(sent));

  // ── Add Device step 3 under the new scope ──
  await wizardToStep3(page, '127.0.0.1');
  await nameIt(page, 'Scoped meter');
  check('Name & save: the topic placeholder is the scoped default',
        await page.getAttribute('#adOutPrefix', 'placeholder') === 'mbg/site-e2e/devices/scoped-meter',
        await page.getAttribute('#adOutPrefix', 'placeholder'));
  check('Name & save: the bucket placeholder is the scoped default',
        await page.getAttribute('#adOutBucket', 'placeholder') === 'site-e2e-scoped-meter',
        await page.getAttribute('#adOutBucket', 'placeholder'));
  await page.waitForSelector('#adReviewBody .ad-strip', { timeout: 10000 }).catch(() => {});
  check('Name & save: the review strip shows where it publishes (the dry run\'s word)',
        /mbg\/site-e2e\/devices\/scoped-meter/.test(await page.locator('#adReviewBody').innerText()));
  await shot(page, 'add-save-scoped');
  await create(page, {});
  await page.click('#adResDone');
  const made = (await api('/api/devices/scoped-meter')).device || {};
  check('created with the scoped topic and bucket',
        made.mqtt_topic_prefix === 'mbg/site-e2e/devices/scoped-meter' && made.influxdb_bucket === 'site-e2e-scoped-meter',
        `${made.mqtt_topic_prefix} ${made.influxdb_bucket}`);
  check('its connection_spec is there (read only)', made.connection_spec?.protocol === 'modbus');
  // ── a refused prefix, at its field ──
  await wizardToStep3(page, '127.0.0.1');
  await nameIt(page, 'Squatter');
  await page.click('#adOutMqtt summary');
  const n = page.codes.length;
  await page.fill('#adOutPrefix', 'mbg/site-e2e/devices/scoped-meter/sub');
  await page.locator('#adOutPrefix').dispatchEvent('input');
  await page.waitForFunction(() => document.getElementById('adOutPrefix')?.getAttribute('aria-invalid') === 'true', null, { timeout: 10000 });
  check('the refusal is the dry run\'s 422', page.codes.slice(n).some(([m, p, s]) => m === 'POST' && p === '/api/devices' && s === 422));
  check('Create waits (aria-disabled), the reason beside it', await page.getAttribute('#addDevNext', 'aria-disabled') === 'true'
        && /scoped-meter/.test(await page.locator('#adRefuse').innerText()));
  await page.click('#addDevNext', { force: true });   // aria-disabled still answers: it says why
  await page.waitForTimeout(500);
  check('…pressed anyway, it stays on Name & save', await step(page) === 'save');
  const topicErr = await describedBy(page, '#adOutPrefix');
  check('the topic field says whose prefix it would sit inside', /inside device 'scoped-meter'/.test(topicErr), topicErr);
  check('nothing was created', (await fetch(BASE + '/api/devices/squatter')).status === 404);
  await shot(page, 'add-prefix-refused');
  await page.evaluate(() => { window.app._ad.done = true; window.app.closeModal('addDeviceModal'); });

  // ── the device page still saves (the Edit modal, with the tag it read) ──
  await page.evaluate(() => window.app.openDeviceDetail('scoped-meter'));
  await openDeviceTab(page, 'connection');
  await page.waitForSelector('#ddvConn fieldset', { state: 'attached' });
  await page.fill('#ddvUnit', '9');
  const m = page.codes.length;
  const saved = await saveDeviceEditor(page);
  const puts = page.codes.slice(m).filter(([mm, p, s]) => mm === 'PATCH' && p === '/api/devices/scoped-meter');
  check('the device page saves: 200, never 412', saved && saved.status() === 200 && puts.every(([, , s]) => s === 200), JSON.stringify(puts));
  check('the change is stored', ((await api('/api/devices/scoped-meter')).device || {}).unit_id === 9);
  const tag = (await fetch(BASE + '/api/devices/scoped-meter')).headers.get('etag');
  check('GET one device carries an ETag', !!tag && tag.startsWith('"'));

  // ── Romanian ──
  await page.evaluate(() => window.app.setLanguage('ro'));
  await page.waitForTimeout(600);
  await general(page);
  check('ro: the card in Romanian', /Identitatea gateway-ului/.test(await page.locator('[data-i18n="identity.title"]').first().innerText())
        && /Cum se denumesc dispozitivele noi/.test(await page.locator('label[for="cfgIdentityScope"]').innerText()));
  check('ro: the scope hint', /doar dispozitivele create de acum înainte/.test(await describedBy(page, '#cfgIdentityScope')));
  check('ro: the example', /Un dispozitiv creat acum/.test(await page.locator('#cfgIdentityExample').innerText())
        && /mbg\/site-e2e\/devices\/\{device\}/.test(await page.locator('#cfgIdentityExample').innerText()));
  await shot(page, 'identity-ro');
  await page.evaluate(() => window.app.setLanguage('en'));

  // ── a phone ──
  const phone = await open({ width: 390, height: 844 });
  await general(phone);
  const sideways = await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  check('phone: no sideways scroll on the card', !sideways);
  await shot(phone, 'identity-phone');
  check('no page errors', !page.errs.length && !phone.errs.length, [...page.errs, ...phone.errs].join(' | '));
} catch (e) {
  check('ran to the end', false, String(e && e.stack || e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
