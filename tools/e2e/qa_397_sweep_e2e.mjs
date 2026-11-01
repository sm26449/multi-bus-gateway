// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// QA sweep for a release (3.97): every page, every tab, every device tab and
// every dialog reachable from them by an "open/add/edit…" control, in en and
// ro, at desktop (1440×900) and phone (390×844) width, light and dark. Not a
// pass/fail test: a report of what each screen shows wrong —
// - console errors/warnings, uncaught exceptions, failed requests (≥400);
// - horizontal page scroll, and the elements that stick out past the viewport;
// - text clipped by its own box (overflow hidden, no ellipsis);
// - raw i18n keys; on ro, English left over (an en string whose ro differs,
//   or a run of English words);
// - controls without an accessible name;
// - text under 4.5:1 contrast (3:1 when large) against its painted background;
// - visible cards with nothing in them;
// - per dialog: focus kept inside on Tab, Esc closes it, focus returns.
//
// A throwaway gateway (127.0.0.1 only, nothing real reachable):
//   MBG_FX_NAME=qa4-types tools/e2e/fixture_up.sh types 18540
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18540 OUT=/tmp/qa node tools/e2e/qa_397_sweep_e2e.mjs
//   docker rm -f qa4-types
// Env: LANGS=en,ro  WIDTHS=1440,390  THEMES=light,dark (or COMBOS=en/1440/light,…)  ONLY=<regex on screen name>
//      PAGES=a,b  DEVS=<regex>  PARTS=pages,devices,installations,wizards narrow the walk
//      DIALOGS=0 skips the dialog walk; SHOTS=1 screenshots every screen with a problem.
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const B = process.env.MBG_URL || 'http://127.0.0.1:18540';
const OUT = process.env.OUT || '/tmp/qa-sweep';
const LANGS = (process.env.LANGS || 'en,ro').split(',');
const WIDTHS = (process.env.WIDTHS || '1440,390').split(',').map(Number);
const THEMES = (process.env.THEMES || 'light,dark').split(',');
const ONLY = process.env.ONLY ? new RegExp(process.env.ONLY) : null;
const DIALOGS = process.env.DIALOGS !== '0';
const SHOTS = process.env.SHOTS === '1';
const PAGES = process.env.PAGES ? process.env.PAGES.split(',') : null;   // narrow the walk while debugging
const DEVS = process.env.DEVS ? new RegExp(process.env.DEVS) : null;
const PARTS = (process.env.PARTS || 'pages,devices,installations,wizards').split(',');
fs.mkdirSync(OUT, { recursive: true });

const en = JSON.parse(fs.readFileSync(path.join(HERE, '../../ui/languages/en.json')));
const ro = JSON.parse(fs.readFileSync(path.join(HERE, '../../ui/languages/ro.json')));
const KEYS = Object.keys(en).filter(k => !k.startsWith('_'));
// an en string that must not appear on a ro page: its ro differs, and it is
// a phrase (a lone "OK", "MQTT", "kWh" is the same in both)
const EN_ONLY = (() => {
  const same = new Set(KEYS.filter(k => typeof en[k] === 'string' && ro[k] === en[k]).map(k => en[k].trim()));
  return [...new Set(KEYS.filter(k => typeof en[k] === 'string' && ro[k] && ro[k] !== en[k])
    .map(k => en[k].replace(/<[^>]+>/g, '').trim())
    .filter(s => s.length >= 4 && /[a-z]{3}/.test(s) && !/\{/.test(s) && !same.has(s)))];   // a word ro also keeps ("Host", "Export") is not English left over
})();
const api = async (p) => { try { const r = await fetch(B + p); return r.ok ? r.json() : null; } catch { return null; } };
const devs = ((await api('/api/devices')) || {}).devices || [];
const eps = ((await api('/api/endpoints')) || {}).endpoints || [];

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const report = [];
let shotN = 0;

// ─────────────────────────── the in-page audit ────────────────────────────
function pageAudit({ keys, enOnly, lang, scopeSel }) {
  const root = scopeSel ? document.querySelector(scopeSel) : document.body;
  if (!root) return { missingScope: scopeSel };
  const vis = e => {
    if (!(e instanceof Element)) return false;
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return false;
    return !!(e.offsetParent || cs.position === 'fixed' || e.tagName === 'BODY');
  };
  // anything under a closed modal / hidden panel is not on screen
  const shown = e => { for (let x = e; x; x = x.parentElement) { if (x.hidden) return false; if (x.classList?.contains('modal') && !x.classList.contains('active')) return false; } return vis(e); };
  const sel = e => {
    let s = e.tagName.toLowerCase();
    if (e.id) return s + '#' + e.id;
    const cls = [...e.classList].slice(0, 2).join('.');
    if (cls) s += '.' + cls;
    const p = e.parentElement;
    const pid = p && (p.id ? '#' + p.id : (p.classList[0] ? '.' + p.classList[0] : p.tagName.toLowerCase()));
    return (pid ? pid + ' > ' : '') + s;
  };
  const txt = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  const keySet = new Set(keys);
  const leafs = [...root.querySelectorAll('*')].filter(e => !['SCRIPT', 'STYLE', 'OPTION', 'svg', 'path', 'TEMPLATE'].includes(e.tagName)
    && [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) && shown(e));
  const out = {};

  // raw i18n keys (exact key, or key-shaped)
  out.rawKeys = [...new Set(leafs.map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(' ').trim())
    .filter(t => keySet.has(t) || /^[a-z]+(\.[a-zA-Z0-9_]+){2,}$/.test(t)))].slice(0, 10);
  // attributes holding raw keys
  out.rawKeyAttrs = [...new Set([...root.querySelectorAll('[aria-label],[title],[placeholder]')].filter(shown)
    .flatMap(e => ['aria-label', 'title', 'placeholder'].map(a => e.getAttribute(a)).filter(v => v && keySet.has(v.trim()))))].slice(0, 10);

  // horizontal page scroll + who sticks out
  const W = document.documentElement.clientWidth;
  out.hscroll = document.documentElement.scrollWidth > W + 1;
  const inScroller = e => { for (let x = e.parentElement; x && x !== document.body; x = x.parentElement) { const o = getComputedStyle(x).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return true; } return false; };
  out.sticksOut = [...root.querySelectorAll('*')].filter(e => shown(e) && e.getBoundingClientRect().right > W + 2 && !inScroller(e))
    .filter((e, _, all) => !all.includes(e.parentElement)).slice(0, 6).map(e => `${sel(e)} (${Math.round(e.getBoundingClientRect().right)}px)`);

  // clipped text: a box that hides what it holds, without saying so
  out.clipped = leafs.filter(e => {
    const cs = getComputedStyle(e);
    if (cs.textOverflow === 'ellipsis' || e.matches('input,select,textarea,pre,code,.sr-only,.visually-hidden')) return false;
    if (e.clientWidth <= 1 || cs.clip !== 'auto' || cs.clipPath !== 'none') return false;   // hidden on purpose
    const hid = v => v === 'hidden' || v === 'clip';
    return (hid(cs.overflowX) && e.scrollWidth > e.clientWidth + 2) || (hid(cs.overflowY) && e.scrollHeight > e.clientHeight + 3 && cs.webkitLineClamp === 'none');
  }).slice(0, 6).map(e => `${sel(e)} "${txt(e).slice(0, 40)}"`);

  // overlapping text: sibling leaves of one parent whose boxes cross
  const ov = [];
  const byParent = new Map();
  leafs.forEach(e => { const p = e.parentElement; if (!byParent.has(p)) byParent.set(p, []); byParent.get(p).push(e); });
  for (const sibs of byParent.values()) {
    for (let i = 0; i < sibs.length && ov.length < 5; i++) for (let j = i + 1; j < sibs.length; j++) {
      const a = sibs[i].getBoundingClientRect(), b = sibs[j].getBoundingClientRect();
      const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left), iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      const blk = x => { const c = getComputedStyle(x); return c.position !== 'absolute' && c.display !== 'inline'; };
      if (ix > 4 && iy > 4 && blk(sibs[i]) && blk(sibs[j])) { ov.push(`${sel(sibs[i])} × ${sel(sibs[j])}`); break; }
    }
  }
  out.overlap = ov;

  // accessible names
  const nameOf = e => {
    const lb = e.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(txt).join(' '); if (t) return t; }
    const al = e.getAttribute('aria-label'); if (al && al.trim()) return al;
    if (e.matches('input,select,textarea')) {
      if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l && txt(l)) return txt(l); }
      const wrap = e.closest('label'); if (wrap && txt(wrap)) return txt(wrap);
      if (e.matches('input[type=button],input[type=submit]') && e.value) return e.value;
      if (e.title) return e.title;
      if (e.placeholder) return 'PH:' + e.placeholder;   // a name that vanishes once typed into
      return '';
    }
    const t = txt(e); if (t) return t;
    if (e.title) return e.title;
    const img = e.querySelector('img[alt]'); if (img && img.alt) return img.alt;
    return '';
  };
  const ctrls = [...root.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=tab], [role=switch], [role=checkbox]')]
    .filter(e => !e.hidden && !e.closest('[aria-hidden="true"]'))
    .filter(e => shown(e) || (e.matches('input[type=checkbox],input[type=radio]') && e.parentElement && shown(e.parentElement)));
  out.noName = ctrls.filter(e => !nameOf(e)).slice(0, 8).map(sel);
  out.placeholderOnly = ctrls.filter(e => nameOf(e).startsWith('PH:')).slice(0, 8).map(sel);

  // contrast: text vs. the first painted background up the tree
  const rgba = s => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null; const v = m[1].split(/[,\s/]+/).filter(Boolean).map(Number); return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 }; };
  const lum = c => { const f = x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const blend = (top, under) => ({ r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1 });
  const bgOf = e => {
    const stack = [];
    for (let x = e; x; x = x.parentElement) {
      const cs = getComputedStyle(x);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;   // gradient/image: unknown
      const c = rgba(cs.backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a >= 1) break; }
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    if (!stack.length || stack[stack.length - 1].a < 1) base = rgba(getComputedStyle(document.body).backgroundColor) || base;
    return stack.reverse().reduce((u, t) => blend(t, u), base);
  };
  out.lowContrast = leafs.map(e => {
    const cs = getComputedStyle(e);
    if (e.closest('[disabled],[aria-disabled="true"],.disabled')) return null;   // WCAG exempts inactive controls
    const fg = rgba(cs.color); if (!fg) return null;
    const bg = bgOf(e); if (!bg) return null;
    const op = [...(function* () { for (let x = e; x; x = x.parentElement) yield Number(getComputedStyle(x).opacity); })()].reduce((a, b) => a * b, 1);
    const f = blend({ ...fg, a: fg.a * op }, bg);
    const L1 = lum(f), L2 = lum(bg);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const px = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
    const need = (px >= 24 || (px >= 18.6 && bold)) ? 3 : 4.5;
    return ratio < need ? { s: `${sel(e)} "${txt(e).slice(0, 30)}" ${ratio.toFixed(2)}:1 (${cs.color} on rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)}))`, ratio } : null;
  }).filter(Boolean).sort((a, b) => a.ratio - b.ratio).slice(0, 8).map(x => x.s);

  // English on a ro page
  if (lang === 'ro') {
    const enSet = new Set(enOnly);
    const STOP = /\b(the|and|with|from|this|that|which|when|will|your|have|has|been|there|here|into|only|each|every|what|where|not|are|is|of|to|for|on|in|be|or|it|no|by|as)\b/gi;
    const RO = /[ăâîșțşţ]|\b(și|sau|de|la|cu|în|un|o|pe|nu|este|dacă|pentru|din)\b/i;
    const seen = new Set();
    out.english = [];
    const consider = (t, where) => {
      t = t.replace(/\s+/g, ' ').trim();
      if (!t || seen.has(t)) return;
      seen.add(t);
      const stop = (t.match(STOP) || []).length;
      if (enSet.has(t) || (stop >= 2 && !RO.test(t) && t.split(' ').length >= 3)) out.english.push(`${where} "${t.slice(0, 70)}"`);
    };
    leafs.forEach(e => consider([...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' '), sel(e)));
    [...root.querySelectorAll('[aria-label],[title],[placeholder]')].filter(shown).forEach(e =>
      ['aria-label', 'title', 'placeholder'].forEach(a => { const v = e.getAttribute(a); if (v) consider(v, `${sel(e)}[${a}]`); }));
    out.english = out.english.slice(0, 15);
  }

  // empty cards / panels on screen
  out.emptyCards = [...root.querySelectorAll('.settings-card-body, .card-body, .card, .empty-state, [role=tabpanel]')]
    .filter(e => shown(e) && !txt(e) && !e.querySelector('input,select,textarea,canvas,svg,img,button')).slice(0, 5).map(sel);
  return out;
}

// ─────────────────────────── driving ──────────────────────────────────────
async function newPage(lang, width, theme) {
  const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, colorScheme: theme,
    deviceScaleFactor: 1, isMobile: false, hasTouch: width < 600 });
  await ctx.addInitScript(([l, t]) => { try { localStorage.setItem('mbg-lang', l); localStorage.setItem('mbg-theme', t); } catch (e) { /* */ } }, [lang, theme]);
  const p = await ctx.newPage();
  p.setDefaultTimeout(8000);
  p._ev = [];
  p.on('console', m => { if (['error', 'warning'].includes(m.type())) p._ev.push(`console.${m.type()}: ${m.text().slice(0, 200)}`); });
  p.on('pageerror', e => p._ev.push(`pageerror: ${String(e).slice(0, 200)}`));
  p.on('response', r => { if (r.status() >= 400) p._ev.push(`HTTP ${r.status()} ${r.request().method()} ${r.url().replace(B, '')}`); });
  p.on('requestfailed', r => { if (!/aborted/i.test(r.failure()?.errorText || '')) p._ev.push(`requestfailed ${r.url().replace(B, '')} ${r.failure()?.errorText}`); });
  p.on('dialog', d => { p._ev.push(`native ${d.type()}: ${d.message().slice(0, 80)}`); d.dismiss().catch(() => {}); });
  return p;
}

let navN = 0;
async function goRoute(p, route) {
  // a fresh document every time (a query that changes): a same-URL goto is a no-op
  await p.goto(`${B}/?n=${++navN}${route}`, { waitUntil: 'load' });
  await p.waitForFunction(() => window.app && window.app._routeReady, null, { timeout: 15000 }).catch(() => {});
  await p.waitForTimeout(900);
  // the first-run offer, when it shows
  if (await p.locator('#firstRunModal.active').count()) await p.evaluate(() => app.closeModal('firstRunModal'));
}

async function audit(p, combo, name, extra = {}) {
  if (ONLY && !ONLY.test(name)) { p._ev.length = 0; return; }
  await p.waitForTimeout(extra.wait ?? 600);
  let info;
  try {
    info = await p.evaluate(pageAudit, { keys: KEYS, enOnly: combo.lang === 'ro' ? EN_ONLY : [], lang: combo.lang, scopeSel: extra.scope || null });
  } catch (e) { info = { auditError: String(e).slice(0, 200) }; }
  const events = p._ev.splice(0);
  const rec = { name, ...combo, ...info, events, ...(extra.dialog || {}) };
  const bad = Object.entries(rec).some(([k, v]) => Array.isArray(v) ? v.length : (k === 'hscroll' && v) || k === 'auditError' || (k === 'escCloses' && v === false) || (k === 'trapOk' && v === false) || (k === 'focusBack' && v === false));
  if (bad && SHOTS) {
    rec.shot = `${OUT}/${String(++shotN).padStart(4, '0')}-${name.replace(/[^\w.-]+/g, '_')}-${combo.lang}-${combo.width}-${combo.theme}.png`;
    await p.screenshot({ path: rec.shot, fullPage: false, timeout: 5000, animations: 'disabled' }).catch(() => { rec.shot = null; });
  }
  report.push(rec);
  process.stdout.write(bad ? '!' : '.');
}

// sub-tabs of the page on screen (two levels), each clicked and audited
const TAB_SEL = '.page.active .config-main-tab:not([data-dtab]):not([aria-disabled="true"]), .page.active [role=tab]:not(.nav-tab):not([data-dtab])';
async function walkTabs(p, combo, base, depth = 0, seen = new Set()) {
  const ids = await p.$$eval(TAB_SEL, els => els.filter(e => e.offsetParent && !e.hidden).map(e => {
    const a = [...e.attributes].find(x => /^data-(cfgtab|tmtab|regtab|vmtab|args)$/.test(x.name)) || null;
    return e.id ? `#${e.id}` : (a ? `[${a.name}='${a.value.replace(/'/g, "\\'")}']` : null);
  }).filter(Boolean));
  for (const s of ids) {
    if (seen.has(s)) continue;
    seen.add(s);
    const loc = p.locator(s).first();
    if (!(await loc.isVisible().catch(() => false))) continue;
    await loc.click().catch(() => {});
    const label = (s.match(/='([^']+)'/) || [, s.replace(/^#/, '')])[1];
    await audit(p, combo, `${base}/${label}`);
    if (depth < 1) await walkTabs(p, combo, `${base}/${label}`, depth + 1, seen);
  }
}

// a dialog: audit it, then Tab ×25 must stay inside, Esc closes, focus returns
async function checkDialog(p, combo, name, opener) {
  const before = await p.$$eval('.modal.active', ms => ms.map(m => m.id));
  await opener();
  await p.waitForTimeout(700);
  const after = await p.$$eval('.modal.active', ms => ms.map(m => m.id));
  const opened = after.find(id => !before.includes(id));
  if (!opened) return null;
  await p.evaluate(() => { window.__opener = window.app && document.getElementById(document.querySelector('.modal.active')?.id)?._returnFocus; });
  const sc = `#${opened}`;
  // tab trap
  let trapOk = true;
  for (let i = 0; i < 12; i++) {
    await p.keyboard.press('Tab');
    const inside = await p.evaluate(id => { const a = document.activeElement; const m = document.getElementById(id); return !!(m && m.contains(a)); }, opened);
    if (!inside) { trapOk = false; break; }
  }
  await audit(p, combo, `${name} ⟨${opened}⟩`, { scope: sc, dialog: { trapOk } });
  // Esc from the dialog's first field, so a picker's own Esc is not in the way
  await p.evaluate(id => { const m = document.getElementById(id); (m.querySelector('.modal-content') || m).focus(); }, opened);
  await p.keyboard.press('Escape');
  await p.waitForTimeout(400);
  const escCloses = !(await p.locator(`${sc}.active`).count());
  const focusBack = escCloses ? await p.evaluate(() => { const o = window.__opener; return !o || !o.isConnected || document.activeElement === o; }) : null;
  if (!escCloses) await p.evaluate(id => app.closeModal(id), opened).catch(() => {});
  if (!escCloses || focusBack === false) report.push({ name: `${name} ⟨${opened}⟩ [keyboard]`, ...combo, escCloses, focusBack, trapOk, events: [] });
  return opened;
}

// a control that opens something: its id, action or (en) words say so
const OPEN_RE = /^(open|show|add|new|import|upload|customize|discover|view|edit|browse|pick|scan|duplicate|clone|create|bridge)|(add|new|import|upload|edit|view|customi[sz]e|discover|bridge|raw|history|details?|more|setkind|kind|type)(btn)?$/i;
const DENY_RE = /delete|remove|restart|reboot|reset|logout|apply|save|test|probe|run\b|arm|send|write|toggle|enable|disable|export|download|close|cancel|copy|refresh|reload|clear|stop|ack|dismiss|swap|sort|filter|lang|theme|find-|chip|capture|pause|read$|^ss|first ?run/i;
async function walkOpeners(p, combo, base, route, scope = '.page.active', prep = null) {
  if (prep) await prep();
  const cands = await p.$$eval(`${scope} button, ${scope} [role=button], ${scope} a[data-action]`, els => {
    const seen = new Set(), out = [];
    for (const e of els) {
      if (!e.offsetParent || e.disabled || e.closest('.modal')) continue;
      const key = e.id || e.dataset.action || '';
      const words = (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 40);
      const k = key || words;
      if (!k || seen.has(k)) continue;
      seen.add(k);
      out.push({ sel: e.id ? `#${CSS.escape(e.id)}` : (e.dataset.action ? `[data-action="${e.dataset.action}"]` : (words && !/"/.test(words) ? `${e.tagName.toLowerCase()}:text-is("${words}")` : null)), key, words });
    }
    return out;
  });
  const pick = cands.filter(c => c.sel && (OPEN_RE.test(c.key) || /^(\+ ?)?(add|new|import|upload|edit|view|customi[sz]e|discover)\b/i.test(c.words))
    && !DENY_RE.test(c.key) && !DENY_RE.test(c.words));
  for (const c of pick.slice(0, 20)) {
    await goRoute(p, route);
    if (prep) await prep();
    const loc = p.locator(`${scope} ${c.sel}`).filter({ visible: true }).first();
    if (!(await loc.count())) continue;
    const opened = await checkDialog(p, combo, `${base}+${(c.key || c.words).replace(/\s+/g, '_')}`, () => loc.click({ timeout: 3000 }).catch(() => {}));
    if (!opened) p._ev.length = 0;   // inline: nothing opened
  }
}

// ─────────────────────────── the walk ─────────────────────────────────────
async function sweep(combo) {
  const p = await newPage(combo.lang, combo.width, combo.theme);
  const pages = ['dashboard', 'devices', 'rules', 'templates', 'status', 'config', 'vmeters', 'diagnostics'];
  for (const pg of (PARTS.includes('pages') ? (PAGES || pages) : [])) {
    await goRoute(p, `#/${pg}`);
    await audit(p, combo, `page:${pg}`, { wait: 1200 });
    await walkTabs(p, combo, `page:${pg}`);
    if (pg === 'config') {
      // Settings: every tab, and inside each the strip it has (Security: Network, Accounts…)
      const cfgTabs = await p.$$eval('.page.active [data-cfgtab]', els => els.filter(e => !e.hidden).map(e => e.dataset.cfgtab));
      for (const ct of cfgTabs) {
        await goRoute(p, '#/config');
        await p.locator(`.page.active [data-cfgtab='${ct}']`).click().catch(() => {});
        await audit(p, combo, `page:config:${ct}`);
        const inner = await p.$$eval('.page.active .st-tab[id]', els => els.filter(e => e.offsetParent).map(e => '#' + e.id));
        for (const it of inner) {
          await p.locator(it).click().catch(() => {});
          await audit(p, combo, `page:config:${ct}/${it.slice(1)}`);
        }
      }
    }
    if (DIALOGS) {
      await goRoute(p, `#/${pg}`);
      await walkOpeners(p, combo, `page:${pg}`, `#/${pg}`);
      const subs = await p.$$eval('.page.active [data-tmtab], .page.active [data-cfgtab], .page.active [data-vmtab], .page.active .config-main-tab[id]:not(.nav-tab)',
        els => els.filter(e => e.offsetParent && !e.classList.contains('active')).map(e => e.dataset.tmtab ? `[data-tmtab='${e.dataset.tmtab}']` : e.dataset.cfgtab ? `[data-cfgtab='${e.dataset.cfgtab}']` : e.dataset.vmtab ? `[data-vmtab='${e.dataset.vmtab}']` : `#${e.id}`));
      for (const t of subs) {
        await walkOpeners(p, combo, `page:${pg}/${t.replace(/.*='|'\]|#/g, '')}`, `#/${pg}`, '.page.active',
          async () => { await p.locator(`.page.active ${t}`).first().click().catch(() => {}); await p.waitForTimeout(500); });
      }
    }
  }
  // devices: Find facets open, list view
  await goRoute(p, '#/devices');
  const ft = p.locator('.find-bar[data-find="devices"] .find-facets-toggle');
  if (await ft.count()) { await ft.first().click().catch(() => {}); await audit(p, combo, 'devices:find-facets'); }
  await goRoute(p, '#/devices?view=list'); await audit(p, combo, 'devices:list-view');
  await goRoute(p, '#/templates?view=list'); await audit(p, combo, 'templates:list-view');
  // every device, every tab
  for (const d of devs.filter(x => PARTS.includes('devices') && (!DEVS || DEVS.test(x.id)))) {
    await goRoute(p, `#/devices/${encodeURIComponent(d.id)}`);
    const tabs = await p.$$eval('#deviceWsTabs [data-dtab]', els => els.map(e => ({ n: e.dataset.dtab, off: e.getAttribute('aria-disabled') === 'true' })));
    for (const t of tabs) {
      if (t.off) continue;
      await goRoute(p, `#/devices/${encodeURIComponent(d.id)}/${t.n}`);
      await audit(p, combo, `device:${d.id}/${t.n}`, { wait: 1000 });
      // nested strips (Outputs → MQTT/Influx/…)
      const sub = await p.$$eval('#deviceDetailView [role=tab]:not([data-dtab]):not([data-dgroup]), #deviceDetailView .config-main-tab:not([data-dtab]):not([data-dgroup])', els => els.filter(e => e.offsetParent && e.id).map(e => '#' + e.id));
      for (const s of sub) {
        await p.locator(s).first().click().catch(() => {});
        await audit(p, combo, `device:${d.id}/${t.n}/${s.slice(1)}`);
      }
      if (DIALOGS && /^(overview|setup|measurements|calculated|commands)$/.test(t.n) && d === devs[0] || (DIALOGS && t.n === 'overview')) {
        await walkOpeners(p, combo, `device:${d.id}/${t.n}`, `#/devices/${encodeURIComponent(d.id)}/${t.n}`, '#deviceDetailView');
      }
    }
    // the editors that are no longer tabs (L3.3: ⋯ → …; L3.4: the Edit modal), each opened and audited
    for (const ed of ['connection', 'polling', 'rename', 'outputs', 'about', 'meters']) {
      await goRoute(p, `#/devices/${encodeURIComponent(d.id)}`);
      const has = await p.evaluate(k => {
        if (!document.querySelector(`#ddvActionsMenu [data-ddv-act="${k}"]`)) return false;
        window.app.ddvEdit(k);
        return true;
      }, ed);
      if (has) await audit(p, combo, `device:${d.id}/edit:${ed}`, { wait: 1000 });
    }
    // the ⋯ actions menu and what it opens
    if (DIALOGS && (await p.locator('#ddvActions').count())) {
      await goRoute(p, `#/devices/${encodeURIComponent(d.id)}`);
      await p.click('#ddvActions').catch(() => {});
      await audit(p, combo, `device:${d.id}/actions-menu`);
      // an item is named by what it does (data-ddv-act, L3.3), else its id
      const items = await p.$$eval('#ddvActionsMenu [role=menuitem], #ddvActionsMenu button', els => els.filter(e => e.offsetParent).map(e => e.dataset.ddvAct || e.id || e.dataset.action).filter(Boolean));
      if (process.env.DEBUG) console.log('\nmenu items', d.id, items);
      for (const it of items) {
        // never what changes the device (a write at once, or a live action)
        if (/delete|remove|disable|enable|restart|duplicate|primary|lock|reconnect|^test$/i.test(it)) continue;
        await goRoute(p, `#/devices/${encodeURIComponent(d.id)}`);
        await p.click('#ddvActions').catch(() => {});
        await p.waitForSelector('#ddvActionsMenu:not([hidden])', { timeout: 3000 }).catch(() => {});
        const loc = p.locator(`#ddvActionsMenu [data-ddv-act="${it}"], #ddvActionsMenu #${it}, #ddvActionsMenu [data-action="${it}"]`).first();
        await checkDialog(p, combo, `device:${d.id}/menu:${it}`, () => loc.click({ timeout: 3000 }).catch(() => {}));
        p._ev.length = 0;
      }
    }
  }
  // installations
  for (const e of eps.filter(() => PARTS.includes('installations'))) {
    await goRoute(p, `#/installations/${encodeURIComponent(e.id)}`);
    await audit(p, combo, `installation:${e.id}`, { wait: 1500 });
    if (DIALOGS) await walkOpeners(p, combo, `installation:${e.id}`, `#/installations/${encodeURIComponent(e.id)}`, '#devicesPage');
  }
  // Add Device, step by step (L3.9: What · Connection · Check · Name & save),
  // every port of the Connection step audited; then the plant wizard
  if (DIALOGS && PARTS.includes('wizards')) {
    await goRoute(p, '#/devices');
    await p.locator('#devicesPage [data-action="openDeviceWizard"]').first().click().catch(() => {});
    await p.waitForTimeout(800);
    const M = 'addDeviceModal';
    if (!(await p.locator(`#${M}.active`).count())) report.push({ name: 'wizard:Add Device did not open', ...combo, events: p._ev.splice(0) });
    else {
      await audit(p, combo, `wizard:${M}/what`, { scope: `#${M}` });
      await p.locator(`#${M} [data-ad-way="connection"]`).click().catch(() => {});
      await p.waitForTimeout(600);
      for (const port of ['serial', 'ethernet', 'can', 'http', 'mqtt']) {
        await p.locator(`label:has(#csAddPortKind-${port})`).click().catch(() => {});
        await p.waitForTimeout(500);
        if (port === 'serial') {
          for (const reach of ['here', 'bridge', 'address']) {
            await p.locator(`label:has(#csAddReach-${reach})`).click().catch(() => {});
            await p.waitForTimeout(600);
            await audit(p, combo, `wizard:${M}/connection-serial-${reach}`, { scope: `#${M}` });
            const cbx = p.locator(`#${M} .cbx-btn`).filter({ visible: true }).first();
            if (reach === 'here' && await cbx.count()) {
              await cbx.click().catch(() => {});
              await audit(p, combo, `wizard:${M}/connection-serial-port-picker`, { scope: `#${M}` });
              await p.keyboard.press('Escape');
              await p.waitForTimeout(300);
              if (!(await p.locator(`#${M}.active`).count())) {
                report.push({ name: `wizard:${M}/connection-port-picker [Esc closed the whole dialog]`, ...combo, events: [] });
                break;
              }
            }
          }
        } else await audit(p, combo, `wizard:${M}/connection-${port}`, { scope: `#${M}` });
      }
      // an address nothing answers on: the check refuses or reads nothing, Next goes on
      await p.locator('label:has(#csAddPortKind-ethernet)').click().catch(() => {});
      await p.waitForTimeout(400);
      await p.fill('#csAddHost', '127.0.0.1').catch(() => {});
      await p.locator(`#${M} #addDevNext`).click().catch(() => {});
      await p.waitForTimeout(800);
      await audit(p, combo, `wizard:${M}/what-after-connection`, { scope: `#${M}` });
      const opt = p.locator(`#${M} #adResults [role=option]`).first();
      if (await opt.count()) {
        await opt.click().catch(() => {});
        await p.locator(`#${M} #addDevNext`).click().catch(() => {});
        await p.waitForTimeout(2500);
        await audit(p, combo, `wizard:${M}/check`, { scope: `#${M}` });
        await p.locator(`#${M} #addDevNext`).click().catch(() => {});
        await p.waitForTimeout(1500);
        await audit(p, combo, `wizard:${M}/save`, { scope: `#${M}` });
      }
      await p.evaluate(m => { if (app._ad) app._ad.done = true; app.closeModal(m); }, M).catch(() => {});
    }
  }
  for (const [btn, modal] of [['Add Installation', 'plantWizardModal']]) {
    if (!DIALOGS || !PARTS.includes('wizards')) break;
    await goRoute(p, '#/devices');
    const opener = await p.evaluate(() => document.querySelector('#devicesPage [data-action="openPlantWizard"]') ? '[data-action="openPlantWizard"]' : null);
    if (opener) await p.locator(`#devicesPage ${opener}`).first().click().catch(() => {});
    else await p.evaluate(() => app.openPlantWizard?.()).catch(() => {});
    await p.waitForTimeout(800);
    if (!(await p.locator(`#${modal}.active`).count())) { report.push({ name: `wizard:${btn} did not open`, ...combo, events: p._ev.splice(0) }); continue; }
    const last = 4;
    for (let step = 1; step <= last; step++) {
      await audit(p, combo, `wizard:${modal}/step${step}`, { scope: `#${modal}` });
      if (step === last) break;   // its Next creates: not here
      // what a user types to move on: an address, a name, the first choice of each group
      await p.evaluate(m => {
        const md = document.getElementById(m);
        const FILL = { pwName: 'QA plant', pwId: 'qa-plant', pwHost: '127.0.0.1' };
        for (const [id, v] of Object.entries(FILL)) {
          const i = md.querySelector('#' + id);
          if (i && i.offsetParent && !i.value) { i.value = v; i.dispatchEvent(new Event('input', { bubbles: true })); i.dispatchEvent(new Event('change', { bubbles: true })); }
        }
        const groups = new Set(Array.from(md.querySelectorAll('input[type=radio]')).filter(x => x.offsetParent).map(x => x.name));
        groups.forEach(n => { if (!md.querySelector(`input[type=radio][name="${n}"]:checked`)) md.querySelector(`input[type=radio][name="${n}"]`).click(); });
      }, modal).catch(() => {});
      const sig = await p.$eval(`#${modal}`, m => m.innerText.slice(0, 400));
      const next = p.locator(`#${modal} #plantWizNext`).filter({ visible: true }).first();
      if (!(await next.count())) break;
      await next.click().catch(() => {});
      await p.waitForTimeout(800);
      const sig2 = await p.$eval(`#${modal}`, m => m.innerText.slice(0, 400)).catch(() => sig);
      if (sig2 === sig) { await audit(p, combo, `wizard:${modal}/step${step}-refused`, { scope: `#${modal}` }); break; }
    }
    await p.evaluate(m => app.closeModal(m), modal).catch(() => {});
  }
  await p.context().close();
}

// COMBOS=en/1440/light,ro/390/dark picks combinations instead of the full product
const combos = process.env.COMBOS
  ? process.env.COMBOS.split(',').map(c => { const [lang, width, theme] = c.split('/'); return { lang, width: Number(width), theme }; })
  : LANGS.flatMap(lang => WIDTHS.flatMap(width => THEMES.map(theme => ({ lang, width, theme }))));
for (const combo of combos) {
  const { lang, width, theme } = combo;
  process.stdout.write(`\n${lang} ${width} ${theme} `);
  try { await sweep(combo); } catch (e) { report.push({ name: 'sweep crashed', ...combo, events: [String(e).slice(0, 300)] }); console.log('\n', e); }
}
await browser.close();
fs.writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 1));
const screens = new Set(report.map(r => r.name));
console.log(`\n${report.length} audits over ${screens.size} screens → ${OUT}/report.json`);
