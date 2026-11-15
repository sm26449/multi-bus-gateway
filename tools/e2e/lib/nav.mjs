// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The main navigation (nav UX-13): one row — Dashboard and Status are
// top-level links; the other pages sit in task submenus built with the shared
// menu component (Assets ▾, Configuration ▾, System ▾); on a phone the whole
// row hides behind the "Navigation" disclosure. A page link inside a closed
// submenu is not clickable, so a script that clicks `[data-page="rules"]`
// directly times out (E2E-NAV-1). Open a page the way a user does — through
// this helper — so the next reorganisation changes this file only.
//
//   await openPage(page, 'rules');      // Configuration ▾ → Rules
//   await openPage(page, 'dashboard');  // a top-level link: one click

export async function openPage(page, name) {
  const link = page.locator(`#mainNav [data-page="${name}"]`).first();
  if (!(await link.isVisible())) {
    const disclosure = page.locator('#navDisclosure');
    if (await disclosure.isVisible() && (await disclosure.getAttribute('aria-expanded')) !== 'true') {
      await disclosure.click();
    }
  }
  if (!(await link.isVisible())) {
    // its submenu: the trigger that controls the menu holding the link
    const menuId = await link.evaluate(a => a.closest('[role="menu"]')?.id || '');
    if (menuId) await page.click(`#mainNav [aria-controls="${menuId}"]`);
  }
  await link.click();
  await page.waitForFunction(n => (location.hash || '').replace(/^#\/?/, '').split('/')[0] === n
    || document.querySelector(`#mainNav [data-page="${n}"]`)?.getAttribute('aria-current') === 'page',
  name, { timeout: 5000 });
}

// Open a page AND draw its data again. Clicking the link of the page already
// shown does nothing (app-router: the same hash is no move), so a script that
// "goes to Devices" while on Devices reads what was drawn earlier — a bridge's
// "0 frames", "checking…", a bus not measured yet (E2E-NAV-2). On the page
// already, reload it, as a user would to see it fresh.
export async function freshPage(page, name) {
  const here = await page.evaluate(n => (location.hash || '').replace(/^#\/?/, '').split(/[/?]/)[0] === n, name);
  if (here) {
    await page.reload();
    await page.waitForLoadState('networkidle');
  }
  await openPage(page, name);
}
