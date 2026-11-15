/* Nav IA (Assets regroup): the navigation stays on ONE row. Dashboard and Status
 * are one click away; the rest group into THREE task submenus — Assets
 * (Devices/Virtual Meters/Virtual BMS), Configuration (Rules/Templates) and System
 * (Diagnostics/Settings) — built with the shared menu component, each link with
 * aria-current and the group trigger lit when a child is current. Fits RO/EN at
 * 1440/1100. On a phone everything opens flattened behind a "Navigation" disclosure
 * (Escape closes). Virtual BMS opens its own page.
 *
 *   tools/e2e/fixture_up.sh plant 18085
 *   MBG_URL=http://127.0.0.1:18085 CHROMIUM_PATH=<chrome> node ux13_nav_e2e.mjs
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18085';
const results = [];
const check = (n, c, e = '') => { results.push({ n, ok: !!c }); console.log(`${c ? 'PASS' : 'FAIL'} ${n}${e ? ' — ' + JSON.stringify(e) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function topVisible(page) {
  return page.evaluate(() => {
    const vw = window.innerWidth;
    const items = [...document.querySelectorAll('#mainNav > .nav-tab, #mainNav > .nav-submenu-wrap > .nav-submenu-btn')];
    return items.map(a => { const r = a.getBoundingClientRect();
      return { id: a.dataset.page || a.id, visible: !!a.offsetParent, inView: r.left >= -1 && r.right <= vw + 1 && r.width > 10 }; });
  });
}

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage(); page.setDefaultTimeout(20000);
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});

  check('one row inside the titlebar (no second bar)', await page.evaluate(() =>
    !!document.querySelector('.titlebar .nav-group #mainNav') && !document.querySelector('.nav-bar')));
  check('top-level = Dashboard + Status + three group triggers', await page.evaluate(() => {
    const direct = [...document.querySelectorAll('#mainNav > .nav-tab[data-page]')].map(a => a.dataset.page).join(',');
    return direct === 'dashboard,status'
      && !!document.getElementById('navAssets') && !!document.getElementById('navCfg') && !!document.getElementById('navSys');
  }));
  check('Assets = Devices/Virtual Meters/Virtual BMS', await page.evaluate(() =>
    [...document.querySelectorAll('#navAssetsMenu .nav-tab[data-page]')].map(a => a.dataset.page).join(',') === 'devices,vmeters,vbms'));
  check('Configuration = Rules/Templates', await page.evaluate(() =>
    [...document.querySelectorAll('#navCfgMenu .nav-tab[data-page]')].map(a => a.dataset.page).join(',') === 'rules,templates'));
  check('System = Diagnostics/Settings', await page.evaluate(() =>
    [...document.querySelectorAll('#navSysMenu .nav-tab[data-page]')].map(a => a.dataset.page).join(',') === 'diagnostics,config'));

  let v = await topVisible(page);
  check('EN 1440: every top-level item visible and within the viewport', v.every(x => x.visible && x.inView), v.filter(x => !(x.visible && x.inView)));

  // Assets submenu opens and navigates, lighting its trigger
  await page.click('#navAssets'); await page.waitForTimeout(250);
  check('clicking Assets opens its submenu', await page.locator('#navAssetsMenu').evaluate(el => !el.hidden));
  await page.click('#navAssetsMenu .nav-tab[data-page="devices"]'); await page.waitForTimeout(400);
  check('an Assets item navigates and lights the Assets trigger', await page.evaluate(() =>
    /#\/devices/.test(location.hash)
    && document.querySelector('#navAssetsMenu .nav-tab[data-page="devices"]').getAttribute('aria-current') === 'page'
    && document.getElementById('navAssets').classList.contains('active')
    && !document.getElementById('navCfg').classList.contains('active')
    && !document.getElementById('navSys').classList.contains('active')));

  // the Virtual BMS page is reachable from Assets (a real page since ADR-0037, no placeholder)
  await page.click('#navAssets'); await page.waitForTimeout(200);
  await page.click('#navAssetsMenu .nav-tab[data-page="vbms"]'); await page.waitForTimeout(400);
  check('Virtual BMS opens its page', await page.evaluate(() =>
    /#\/vbms/.test(location.hash)
    && document.getElementById('vbmsPage')?.classList.contains('active')
    && !!document.getElementById('vbmsContent')
    && !document.querySelector('#vbmsPage .route-missing')));

  // one-menu-at-a-time: opening Configuration closes Assets
  await page.click('#navCfg'); await page.waitForTimeout(250);
  check('opening Configuration closes any other open submenu', await page.evaluate(() =>
    !document.getElementById('navCfgMenu').hidden
    && document.getElementById('navAssetsMenu').hidden && document.getElementById('navSysMenu').hidden));
  await page.click('#navCfgMenu .nav-tab[data-page="templates"]'); await page.waitForTimeout(400);
  check('a Configuration item navigates + lights Configuration only', await page.evaluate(() =>
    /#\/templates/.test(location.hash) && document.getElementById('navCfg').classList.contains('active')
    && !document.getElementById('navAssets').classList.contains('active')));

  // Status is a top-level link now; clicking it clears every group trigger
  await page.click('#mainNav > .nav-tab[data-page="status"]'); await page.waitForTimeout(400);
  check('Status is top-level, navigates and clears all group triggers', await page.evaluate(() =>
    /#\/status/.test(location.hash)
    && document.querySelector('#mainNav > .nav-tab[data-page="status"]').getAttribute('aria-current') === 'page'
    && !document.getElementById('navAssets').classList.contains('active')
    && !document.getElementById('navCfg').classList.contains('active')
    && !document.getElementById('navSys').classList.contains('active')));

  // RO 1440 + EN 1100 still fit
  await page.evaluate(() => localStorage.setItem('mbg-lang', 'ro'));
  await page.reload(); await page.waitForLoadState('networkidle'); await page.waitForTimeout(600);
  v = await topVisible(page);
  check('RO 1440: every top-level item visible', v.every(x => x.visible && x.inView), v.filter(x => !(x.visible && x.inView)));
  await page.evaluate(() => localStorage.setItem('mbg-lang', 'en'));
  await page.setViewportSize({ width: 1100, height: 900 }); await page.reload(); await page.waitForLoadState('networkidle'); await page.waitForTimeout(500);
  v = await topVisible(page);
  check('EN 1100: every top-level item visible', v.every(x => x.visible && x.inView), v.filter(x => !(x.visible && x.inView)));

  // mobile: disclosure with everything flattened
  await page.setViewportSize({ width: 390, height: 800 }); await page.waitForTimeout(400);
  const disc = page.locator('#navDisclosure');
  check('phone: Navigation disclosure shown, collapsed', await disc.isVisible() && await disc.getAttribute('aria-expanded') === 'false');
  await disc.click(); await page.waitForTimeout(200);
  check('phone: every destination (all submenus flattened) is reachable', await page.evaluate(() =>
    ['dashboard', 'devices', 'vmeters', 'vbms', 'rules', 'templates', 'diagnostics', 'config', 'status']
      .every(p => { const a = document.querySelector(`#mainNav .nav-tab[data-page="${p}"] span`); return a && a.offsetParent; })));
  check('phone: the group triggers are hidden (flattened)', await page.evaluate(() =>
    !document.getElementById('navAssets').offsetParent && !document.getElementById('navCfg').offsetParent && !document.getElementById('navSys').offsetParent));
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  check('phone: Escape closes and returns focus to the disclosure', await disc.getAttribute('aria-expanded') === 'false'
        && await page.evaluate(() => document.activeElement === document.getElementById('navDisclosure')));

  await ctx.close();
} catch (e) { check('ran', false, String(e).slice(0, 220)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
