// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The components of a device, against a
// gateway that has a device PARENT with components TYPES… already added (the
// sandbox: a Victron GX with its batteries and sensors). It removes one
// component and adds it back, and renames one and puts the name back.
//
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:8099 PARENT=gx-test node tools/e2e/device_components_e2e.mjs
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';
import { openDeviceTab } from './lib/device-page.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:8099';
const PARENT = process.env.PARENT || 'gx-test';
const OUT = process.env.OUT || '';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const shot = async (page, name) => { if (OUT) await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false }); };

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function open(width, height) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.setDefaultTimeout(20000);
  page.dlg = await watchDialogs(page);
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push(m.text()); });
  await page.addInitScript(p => { try { localStorage.removeItem(`mbg-components-open-${p}`); } catch (e) {} }, PARENT);
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  return { page, errs };
}

try {
  const comps = (await (await fetch(`${BASE}/api/devices/${PARENT}/components`)).json()).components || [];
  check('the parent holds components', comps.length >= 2, String(comps.length));
  const { page, errs } = await open(1440, 950);
  await page.evaluate(() => window.app.navigateTo('devices'));
  await page.waitForSelector(`[data-component-toggle="${PARENT}"]`);
  await page.waitForTimeout(500);
  await shot(page, 'comp-list');
  const nested = await page.locator(`[data-component-units="${PARENT}"] .device-row`).count();
  check('Devices: the components are nested under their parent', nested === comps.length, `${nested}/${comps.length}`);
  const topIds = await page.$$eval('#devicesList > .device-row .dev-chip:first-of-type', els => els.map(e => e.textContent.trim()));
  check('Devices: no component at the top level', !comps.some(c => topIds.includes(c.id)), topIds.join());
  check('Devices: the toggle says how many answer', new RegExp(`${comps.length} components`).test(await page.locator(`[data-component-toggle="${PARENT}"]`).innerText()));
  await page.click(`[data-component-toggle="${PARENT}"]`);
  check('Devices: it folds', !(await page.locator(`[data-component-units="${PARENT}"]`).isVisible()));
  await page.click(`[data-component-toggle="${PARENT}"]`);
  check('Devices: and unfolds', await page.locator(`[data-component-units="${PARENT}"]`).isVisible());

  // the parent's page
  await page.evaluate(p => window.app.openDeviceDetail(p), PARENT);
  await page.waitForSelector('#devComponentsCard tr[data-comp]');
  await page.waitForTimeout(400);
  await shot(page, 'comp-card');
  check('parent: the Components card lists them', (await page.locator('#devComponentsCard tr[data-comp]').count()) === comps.length);
  check('parent: the add form offers the template’s types', (await page.locator('#compType option').count()) >= 2);
  await page.selectOption('#compType', 'temperature');
  check('parent: the instance label follows the type', /Sensor instance/.test(await page.locator('#compInstanceLabel').innerText()));

  // find on the device
  await page.click('#compFindBtn');
  await page.waitForSelector('#compFound table', { timeout: 15000 });
  await shot(page, 'comp-found');
  const foundRows = await page.locator('#compFound tbody tr').count();
  check('find: the device names what it holds', foundRows >= comps.length, String(foundRows));
  check('find: what is added says so', (await page.locator('#compFound .sink-pill.ok').count()) === comps.length);

  // remove one, and add it back from what was found
  const victim = comps[comps.length - 1];
  page.dlg.answers.push(true);
  await page.click(`#devComponentsCard tr[data-comp="${victim.id}"] [data-action="removeComponentUi"]`);
  await page.waitForFunction(id => !document.querySelector(`#devComponentsCard tr[data-comp="${id}"]`), victim.id);
  check('remove: the row goes', (await page.locator('#devComponentsCard tr[data-comp]').count()) === comps.length - 1);
  const addBtn = page.locator('#compFound tbody tr', { hasText: victim.instance }).locator('[data-action="addComponentUi"]');
  check('remove: found offers it again', (await addBtn.count()) === 1);
  await addBtn.first().click();
  await page.waitForSelector(`#devComponentsCard tr[data-comp="${victim.id}"]`);
  check('add from found: back, at the same id', (await page.locator('#devComponentsCard tr[data-comp]').count()) === comps.length);
  // its name: what the device calls it, then the one it had
  const added = (await (await fetch(`${BASE}/api/devices/${PARENT}/components`)).json()).components.find(c => c.id === victim.id);
  check('add from found: named as the device names it', !!added && added.name.length > 0, added && added.name);
  page.dlg.answers.push(victim.name);
  await page.click(`#devComponentsCard tr[data-comp="${victim.id}"] [data-action="renameComponentUi"]`);
  await page.waitForFunction(([id, n]) => (document.querySelector(`#devComponentsCard tr[data-comp="${id}"]`) || {}).textContent?.includes(n), [victim.id, victim.name]);
  check('rename', true);

  // the add form refuses a bad instance, from the server
  await page.selectOption('#compType', 'battery');
  await page.fill('#compInstance', 'a/b');
  await page.click('#compAddForm [data-action="addComponentUi"]');
  await page.waitForTimeout(800);
  check('add: a bad instance is refused', (await page.locator('#devComponentsCard tr[data-comp]').count()) === comps.length);

  // a component's own page
  await page.click(`#devComponentsCard tr[data-comp="${comps[0].id}"] a`);
  await page.waitForSelector('#deviceWsTabs');
  await page.waitForTimeout(400);
  await shot(page, 'comp-page');
  const head = await page.locator('#deviceDetailView .section-header').innerText();
  check('component: the breadcrumb names its parent', head.includes(comps[0].name) && /›/.test(head));
  check('component: the banner says what it is part of', /Part of/.test(await page.locator('[data-dpanel="overview"]').innerText()));
  check('component: no delete of its own, an Open device', (await page.locator('#deviceDetailView .header-actions [data-action="deleteDevice"]').count()) === 0
    && (await page.locator('#deviceDetailView .header-actions [data-action="openDeviceDetail"]').count()) === 1);
  await openDeviceTab(page, 'connection');
  check('component: no connection form', (await page.locator('#ddvBroker, #ddvProtoShown').count()) === 0);
  await page.click('#deviceDetailView [data-action="closeDeviceDetail"]');
  await page.waitForSelector('#devComponentsCard tr[data-comp]');
  check('component: Back goes to its parent', await page.locator('#devComponentsCard').isVisible());

  await page.evaluate(() => window.app.setLanguage('ro'));
  await page.waitForTimeout(800);
  await page.evaluate(p => window.app.openDeviceDetail(p), PARENT);
  await page.waitForSelector('#devComponentsCard h3');
  check('Romanian', /Componente/.test(await page.locator('#devComponentsCard h3').innerText()));
  await page.evaluate(() => window.app.setLanguage('en'));
  check('no page errors (desktop)', errs.length === 0, errs.join(' | '));
  await page.close();

  const ph = await open(390, 844);
  await ph.page.evaluate(p => window.app.openDeviceDetail(p), PARENT);
  await ph.page.waitForSelector('#devComponentsCard tr[data-comp]');
  await shot(ph.page, 'comp-phone');
  check('phone: no horizontal page scroll', !(await ph.page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)));
  check('no page errors (phone)', ph.errs.length === 0, ph.errs.join(' | '));
} catch (e) {
  check('ran to the end', false, String(e));
} finally {
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
