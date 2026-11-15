/* UX-07: a rule card separates routine navigation from consequential actions. The
 * read-only "Decisions" leads and is prominent; the overrides (clamp/pause/force)
 * live under one explicit "Overrides" control menu; edit/delete stay secondary
 * (ui/js/app-rules.js). Verified structurally and by opening the menu for real.
 *
 *   tools/e2e/fixture_up.sh drive 18098
 *   MBG_URL=http://127.0.0.1:18098 CHROMIUM_PATH=<chrome> node rules_action_menu_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18098';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});

  const rule = { id: 'ov-u1', label: 'OV u1', kind: 'steps', enabled: true, mode: 'armed',
                 signal: 'x', inputs: [], steps: [{ label: 't', at: 253, value: 80 }],
                 release_below: 250, normal: { value: 100 }, live: { signal: 246, state: 'normal', units: {} } };

  // structural: render and inject the card
  const struct = await page.evaluate(r => {
    const html = app._ruleCardHtml(r, false);
    const host = document.createElement('div');
    host.id = 'ux07host'; host.innerHTML = html; document.body.appendChild(host);
    const header = host.querySelector('.header-actions');
    const btns = [...header.querySelectorAll(':scope > button, :scope > a, :scope > .mbg-menu-wrap > button')];
    const labels = btns.map(b => b.textContent.trim().replace(/\s+/g, ' '));
    const decisions = header.querySelector('button.btn-secondary');
    const ovBtn = header.querySelector('.mbg-menu-wrap > button[aria-haspopup="menu"]');
    const menuItems = [...host.querySelectorAll('.mbg-menu [role="menuitem"]')].map(b => b.textContent.trim());
    // top-level (non-menu) buttons must NOT include clamp/pause/force directly
    const topLevelText = [...header.children].filter(c => !c.classList.contains('mbg-menu-wrap')).map(c => c.textContent).join(' ');
    return { labels, decisionsFirst: /Decisions/.test(labels[0] || ''), decisionsProminent: !!decisions && /Decisions/.test(decisions.textContent),
             hasOverridesMenu: !!ovBtn, menuItems, overridesNotTopLevel: !/Clamp|Pause|Force/.test(topLevelText) };
  }, rule);
  check('Decisions is the first action and prominent (btn-secondary)', struct.decisionsFirst && struct.decisionsProminent, struct.labels);
  check('an explicit Overrides control menu exists', struct.hasOverridesMenu);
  check('clamp/pause/force are menu items, not loose header buttons',
        struct.overridesNotTopLevel && struct.menuItems.length === 3, struct.menuItems);

  // integration: clicking Overrides opens the menu
  const opened = await page.evaluate(() => {
    const btn = document.querySelector('#ux07host .mbg-menu-wrap > button[aria-haspopup="menu"]');
    btn.click();
    const menu = document.getElementById(btn.getAttribute('aria-controls'));
    return { expanded: btn.getAttribute('aria-expanded') === 'true', visible: menu && !menu.hidden };
  });
  check('clicking Overrides opens its menu', opened.expanded && opened.visible, opened);

  await page.evaluate(() => document.getElementById('ux07host')?.remove());
  await ctx.close();
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally {
  await browser.close();
  const n = results.filter(x => !x.ok).length; console.log(`\n${results.length - n}/${results.length} passed`); process.exit(n ? 1 : 0);
}
