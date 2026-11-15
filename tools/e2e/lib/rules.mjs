// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A rule card's manual overrides (Clamp…, Pause…, Force…) sit in its
// "Overrides ▾" menu (UX-07). A menu item inside a closed menu is not
// clickable, so a script that clicks `button:has-text("Force")` on the card
// times out (E2E-NAV-2). Open it the way a user does — through this helper —
// so the next reorganisation of the card changes this file only.
//
//   await ruleOverride(page, 'week-pump', 'force');   // Overrides ▾ → Force…
//   await ruleOverride(page, 'ov-u1', 'clamp');       // … 'pause' → Pause…

const ACTIONS = { clamp: 'openRuleClamp', pause: 'openRuleOverride', force: 'openRuleForce' };

export async function ruleOverride(page, ruleId, what) {
  const action = ACTIONS[what];
  if (!action) throw new Error(`ruleOverride: unknown override "${what}"`);
  const card = page.locator(`[data-rule="${ruleId}"]`).first();
  const item = card.locator(`.mbg-menu [data-action="${action}"]`).first();
  if (!(await item.isVisible())) {
    const menuId = await item.evaluate(b => b.closest('[role="menu"]')?.id || '');
    if (!menuId) throw new Error(`ruleOverride: no overrides menu on rule "${ruleId}"`);
    await card.locator(`[aria-controls="${menuId}"]`).click();
  }
  await item.click();
}
