// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The device page's tabs live in three groups (Live / Configure / Diagnose):
// a tab of another group is out of sight until its group is picked. A
// script opens a tab the way a user does — its group, then the tab — so the
// next reorganisation changes this file only.
//
//   await openDeviceTab(page, 'connection');   // 'edit' is the earlier name
//   await openDeviceGroup(page, 'configure');  // the group's own tab

const ALIAS = { edit: 'connection' };

export async function openDeviceGroup(page, group) {
  await page.click(`#deviceWsGroups [data-dgroup="${group}"]`);
  await page.waitForSelector(`#deviceWsGroups [data-dgroup="${group}"].active`);
}

export async function openDeviceTab(page, tab) {
  const want = ALIAS[tab] || tab;
  const sel = `#deviceWsTabs [data-dtab="${want}"]`;
  await page.waitForSelector(sel, { state: 'attached' });
  const group = await page.getAttribute(sel, 'data-dgroup');
  if (group && !(await page.locator(`#deviceWsGroups [data-dgroup="${group}"].active`).count())) {
    await openDeviceGroup(page, group);
  }
  await page.click(sel);
  await page.waitForSelector(`${sel}.active`);
}

// the device's ⋯ actions menu, opened; then click one of its items
export async function deviceAction(page, itemSel) {
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
  const item = page.locator(`#ddvActionsMenu ${itemSel}`);
  await item.waitFor({ state: 'visible' });
  // an item held back (aria-disabled: in use) still answers a click — it says why
  await item.click({ force: (await item.getAttribute('aria-disabled')) === 'true' });
}
