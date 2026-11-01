// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The device page's tabs live in four groups (Live / Data / Setup /
// Diagnose, ADR-0031 §3): a tab of another group is out of sight until its
// group is picked. Editing is not a tab: it opens from the ⋯ menu (or E) —
// the Edit device modal at a section (connection, polling, identity,
// outputs, model — every kind, the primary, a unit and a component too:
// ADR-0032 §6, L3.11b), a full-screen editor (measurements with calculated,
// commands: one Save in the edit bar) or an editor of the page (meters,
// about). A script opens a tab, or
// an editor, the way a user does, so the next reorganisation changes this
// file only.
//
//   await openDeviceTab(page, 'measurements');  // its group, then the tab (the view)
//   await openDeviceGroup(page, 'setup');       // the group's own tab
//   await openDeviceEditor(page, 'commands');   // ⋯ → Commands & bindings…
//   await openDeviceTab(page, 'connection');    // a tab that left: its editor (⋯)
//   await editorSection(page, 'identity');      // the modal: another section
//   await saveDeviceEditor(page);               // the modal's Save (→ the PATCH answer), or the page form's
//   await closeDeviceEditor(page);              // the modal's Cancel, or Done

const ALIAS = { edit: 'connection' };
// the tabs that only edited (Connection, Outputs, Attributes, Meters): their
// editor, from ⋯ — the menu item that opens it
const EDITORS = { connection: 'connection', outputs: 'outputs', attributes: 'about', meters: 'meters' };

export async function openDeviceGroup(page, group) {
  await page.click(`#deviceWsGroups [data-dgroup="${group}"]`);
  await page.waitForSelector(`#deviceWsGroups [data-dgroup="${group}"].active`);
}

export async function openDeviceTab(page, tab) {
  const want = ALIAS[tab] || tab;
  if (EDITORS[want]) return openDeviceEditor(page, EDITORS[want]);
  const sel = `#deviceWsTabs [data-dtab="${want}"]`;
  await page.waitForSelector(sel, { state: 'attached' });
  if (await editorModalOpen(page) || await page.locator('#ddvEditBar:not([hidden])').count()) await closeDeviceEditor(page);
  const group = await page.getAttribute(sel, 'data-dgroup');
  if (group && !(await page.locator(`#deviceWsGroups [data-dgroup="${group}"].active`).count())) {
    await openDeviceGroup(page, group);
  }
  // a group of one tab (Setup) shows no strip: the group opened it
  if (!(await page.locator(`${sel}.active`).count())) await page.click(sel);
  await page.waitForSelector(`${sel}.active`, { state: 'attached' });
}

// the device's ⋯ actions menu, opened; then click one of its items
export async function deviceAction(page, itemSel) {
  if (await page.locator('#deviceDetailView.ddv-full').count()) await closeDeviceEditor(page);
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
  const item = page.locator(`#ddvActionsMenu ${itemSel}`);
  await item.waitFor({ state: 'visible' });
  // an item held back (aria-disabled: in use) still answers a click — it says why
  await item.click({ force: (await item.getAttribute('aria-disabled')) === 'true' });
}

export const editorModalOpen = async page => (await page.locator('#devEditModal.active').count()) === 1;

// an editor, from ⋯ (item: connection | polling | type | measurements |
// calculated | commands | meters | outputs | about | components | rename |
// edit). A device whose "Edit connection…" cannot run (a unit, a component: their
// name; a meter-only device: Edit…, the modal at its identity) opens what
// stands for it. Waits for the modal or the page's editing bar.
export async function openDeviceEditor(page, item) {
  // a full-screen editor covers the page's ⋯: Done first (it asks about edits not saved)
  if (await page.locator('#deviceDetailView.ddv-full').count()) await closeDeviceEditor(page);
  if ((await page.getAttribute('#ddvActions', 'aria-expanded')) !== 'true') await page.click('#ddvActions');
  await page.waitForSelector('#ddvActionsMenu:not([hidden])');
  let pick = item;
  // (L3.10: the item stays for every kind, disabled where the kind has none)
  const conn = page.locator('#ddvActionsMenu [data-ddv-act="connection"]');
  if (item === 'connection' && (!(await conn.count()) || (await conn.getAttribute('aria-disabled')) === 'true')) {
    pick = (await page.locator('#ddvActionsMenu [data-ddv-act="rename"]').count()) ? 'rename' : 'edit';
  }
  await deviceAction(page, `[data-ddv-act="${pick}"]`);
  await page.waitForSelector('#devEditModal.active .dev-shell-panel:not([hidden]), #ddvEditBar:not([hidden])');
  await page.waitForTimeout(150);
}

// the Edit modal: show a section (model | connection | polling | identity | outputs)
export async function editorSection(page, sec) {
  if (await page.locator(`#devEditTab-${sec}`).isVisible()) await page.click(`#devEditTab-${sec}`);
  else await page.selectOption('#devEditRailSel', sec);
  await page.waitForSelector(`#devEditPanel-${sec}:not([hidden])`);
}

// the modal's footer settled: no dry run running
export async function editorSettled(page) {
  await page.waitForTimeout(450);
  await page.waitForFunction(() => !/Checking|Se verifică/.test(document.getElementById('devEditSummary')?.textContent || ''));
}

// Save: the modal's (answers the PATCH response — the device's, or a unit's
// / a component's own route — null when nothing was sent: no change, a
// refusal, a review cancelled), or a full editor's (its edit bar's Save)
export async function saveDeviceEditor(page) {
  if (!(await editorModalOpen(page))) {
    await page.click('#ddvEditSave');
    return null;
  }
  await editorSettled(page);
  const resp = page.waitForResponse(r => r.request().method() === 'PATCH' && !/dry_run/.test(r.url())
                                       && /\/api\/(devices\/[^/?]+(\/components\/[^/?]+)?|endpoints\/[^/?]+\/units\/[^/?]+)$/.test(new URL(r.url()).pathname), { timeout: 8000 }).catch(() => null);
  await page.click('#devEditSave');
  const r = await resp;
  if (r && r.ok()) {
    await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
    // the page is drawn again from what is stored; it ends on ⋯
    await page.waitForFunction(() => document.activeElement?.id === 'ddvActions', null, { timeout: 8000 }).catch(() => {});
  }
  return r;
}

export async function closeDeviceEditor(page) {
  if (await editorModalOpen(page)) {
    await page.click('#devEditCancel');
    await page.waitForFunction(() => !document.querySelector('#devEditModal.active'));
    return;
  }
  await page.click('#ddvEditDone');
  await page.waitForSelector('#ddvEditBar[hidden]', { state: 'attached' });
}
