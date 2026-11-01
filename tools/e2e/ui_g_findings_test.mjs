// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The guide findings UI-G1..G4, with no browser: the
// page's own scripts run in a sandbox against both dictionaries.
//   G1  a fleet row of a device measured by meters names its values the way
//       its page does (In, Out, Efficiency)
//   G2  Add Device: the line under the connection choice follows the choice
//   G3  History: the unit above the Y axis never sits on the top tick value
//   G4  Monitor: a group is named, never shown as its raw key
//
//   node tools/e2e/ui_g_findings_test.mjs
import { readFileSync } from 'fs';
import vm from 'vm';

const UI = new URL('../../ui/', import.meta.url);
const read = p => readFileSync(new URL(p, UI), 'utf8');
const DICT = { en: JSON.parse(read('languages/en.json')), ro: JSON.parse(read('languages/ro.json')) };

const ctx = {
  console, Intl,
  localStorage: { getItem: () => null, setItem: () => {} },
  document: { getElementById: () => null, body: {}, documentElement: { setAttribute() {} } },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
};
ctx.window = ctx;
vm.createContext(ctx);
for (const f of ['js/connform-model.js', 'js/app-core.js', 'js/app-dashboard.js', 'js/app-fleet.js',
                 'js/app-meters.js', 'js/app-connform.js', 'js/app-history.js', 'js/app-monitor.js']) {
  vm.runInContext(read(f), ctx, { filename: f });
}
vm.runInContext('this.JM = JanitzaMonitor', ctx);
const app = lang => {
  const a = Object.create(ctx.JM.prototype);
  a._t = { ...DICT.en, ...DICT[lang] };
  a._tEn = DICT.en;
  a._lang = lang;
  return a;
};

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const guard = (name, fn) => { try { fn(); } catch (e) { check(name, false, String(e)); } };
const texts = (html, cls) => [...html.matchAll(new RegExp(`class="${cls}"[^>]*>([^<]*)<`, 'g'))].map(m => m[1].trim());

// ── G1: the fleet row of a UPS measured by its meters ──────────────────────
guard('G1', () => {
  const ups = {
    id: 'ups', name: 'Server room UPS', health: 'ok', enabled: true, alarms: {},
    hero: [
      { name: 'power_in', label: 'Input power (UPS input meter)', unit: 'W', value: 882, key: 8000000, meters: true },
      { name: 'power_out', label: 'Output power (UPS output meter)', unit: 'W', value: 818, key: 8000001, meters: true },
      { name: 'efficiency_pct', label: 'efficiency_pct', unit: '', value: null, key: null, meters: true },
    ],
  };
  for (const [lang, want] of [['en', ['In', 'Out', 'Efficiency']], ['ro', [DICT.ro['meters.in'], DICT.ro['meters.out'], DICT.ro['meters.eff']]]]) {
    const a = app(lang);
    a._overviewPins = () => [];
    const html = a._fleetRow(ups);
    check(`G1 ${lang}: the row names its meters' values as its page does`, JSON.stringify(texts(html, 'fm-l')) === JSON.stringify(want), texts(html, 'fm-l'));
    check(`G1 ${lang}: their values are on the row`, /882/.test(html) && /818/.test(html), html);
    check(`G1 ${lang}: which meter stays in the tooltip`, html.includes('title="Input power (UPS input meter)"'));
    check(`G1 ${lang}: a value not given now shows no raw key`, !/>efficiency_pct</.test(html) && !html.includes('title="efficiency_pct"'));
  }
  const a = app('en');
  a._overviewPins = () => [];
  const own = a._fleetRow({ id: 'm', name: 'm', health: 'ok', alarms: {}, hero: [{ name: 'power_in', label: 'Its own power', unit: 'W', value: 5 }] });
  check('G1: a device\'s own value keeps its own label', texts(own, 'fm-l')[0] === 'Its own power', texts(own, 'fm-l'));
  check('G1: the words are the device page\'s words', a._meterValueWord('power_in') === DICT.en['meters.in']
        && a._meterValueWord('efficiency_pct') === DICT.en['meters.eff'] && a._meterValueWord('voltage') === '');
});

// ── G2: the line under "How is it connected?" ───────────────────────────────
guard('G2', () => {
  const draw = (lang, mode, context = 'add', dict = null) => {
    const a = app(lang);
    if (dict) a._t = dict;
    for (const m of ['_cfHttpHtml', '_cfTcpHtml', '_cfRtuHtml', '_cfMqttHtml', '_cfSeplosHtml', '_cfTestHtml']) a[m] = () => '';
    const html = a._cfHtml({ px: 'devWiz', M: { mode }, opts: { context, allowNone: context === 'add' }, orig: { mode } });
    return html;
  };
  // the "Meters only" line (named wizMtr.segHint before UI-G2)
  const noneLine = lang => DICT[lang]['cf.kindHint.none'] || DICT[lang]['wizMtr.segHint'];
  const NONE = noneLine('en');
  for (const lang of ['en', 'ro']) {
    const seen = new Set();
    for (const [mode, kind] of [['tcp', 'tcp'], ['rtu-tcp', 'rtu'], ['rtu', 'rtu'], ['rtu_tap', 'rtu'], ['http', 'http'], ['mqtt', 'mqtt'], ['seplos_v2', 'seplos_v2'], ['none', 'none']]) {
      const want = DICT[lang][`cf.kindHint.${kind}`];
      const html = draw(lang, mode);
      check(`G2 ${lang} ${mode}: its own line`, !!want && html.includes(want), want);
      if (mode !== 'none') check(`G2 ${lang} ${mode}: never the "Meters only" line`, !html.includes(noneLine(lang)));
      seen.add(want);
    }
    check(`G2 ${lang}: every kind says something different`, seen.size === 6, [...seen]);
  }
  const html = draw('en', 'tcp');
  check('G2: the choice is described by the line', /role="radiogroup"[^>]*aria-describedby="devWizKindHint"/.test(html) && html.includes('id="devWizKindHint"'));
  check('G2: editing a device shows no such line', !draw('en', 'tcp', 'edit').includes('cf-kind-hint'));
  const noTcp = { ...DICT.en }; delete noTcp['cf.kindHint.tcp'];
  const bare = draw('en', 'tcp', 'add', noTcp);
  check('G2: a kind with no line in the dictionary shows none', !bare.includes('cf-kind-hint') && !bare.includes(NONE) && !bare.includes('cf.kindHint'));
});

// ── G3: the History chart's Y axis ──────────────────────────────────────────
guard('G3', () => {
  const fake = (W, H) => {
    const calls = [];
    let font = '11px', align = 'left';
    const c2d = {
      setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, closePath() {}, fill() {}, arc() {}, setLineDash() {},
      set font(f) { font = f; }, get font() { return font; },
      set textAlign(a) { align = a; }, get textAlign() { return align; },
      fillText(text, x, y) { calls.push({ text: String(text), x, y, font, align }); },
      measureText: s => ({ width: String(s).length * 6.2 }),
    };
    return { calls, canvas: { clientWidth: W, clientHeight: H, width: 0, height: 0, getContext: () => c2d } };
  };
  for (const [W, H, what] of [[1120, 640, 'desktop'], [340, 300, 'phone']]) {
    const a = app('en');
    a._hexA = () => 'rgba(0,0,0,.1)';
    const { calls, canvas } = fake(W, H);
    const t0 = Date.parse('2026-10-25T00:15:00Z');
    const mean = [0, 1, 2, 3].map(i => ({ t: new Date(t0 + i * 3e5).toISOString(), v: 230.2 + i * 0.4 }));
    a._renderHistory(null, { canvas, legendId: 'none', series: [{ label: 'Voltage L1-N', unit: 'V', color: '#3b82f6', mean, mins: mean, maxs: mean }] });
    const unit = calls.find(c => c.text === 'V');
    const ticks = calls.filter(c => /^-?\d+\.\d$/.test(c.text));
    // an 11 px label spans ~9 px above its baseline and ~3 px below
    const box = c => [c.y - 9, c.y + 3];
    const overlap = c => { const [a1, a2] = box(unit), [b1, b2] = box(c); return a1 < b2 && b1 < a2 && Math.abs(c.x - unit.x) < 40; };
    check(`G3 ${what}: the unit is drawn`, !!unit);
    check(`G3 ${what}: five tick values`, ticks.length === 5, ticks.map(c => c.text));
    check(`G3 ${what}: the unit never sits on a tick value`, unit && !ticks.some(overlap), { unit, top: ticks[ticks.length - 1] });
    check(`G3 ${what}: the unit is inside the canvas`, unit && unit.y - 9 >= 0);
    const times = calls.filter(c => /\d\d:\d\d/.test(c.text));
    check(`G3 ${what}: every time label stays inside the chart`, times.length === 5 && times.every(c => {
      const w = c.text.length * 6.2, x0 = c.align === 'left' ? c.x : c.align === 'right' ? c.x - w : c.x - w / 2;
      return x0 >= 0 && x0 + w <= W;
    }), times);
    const f0 = a._histFrame(false);
    check(`G3 ${what}: with no shared unit, no band is kept for it`, f0.mt < a._histFrame(true).mt);
  }
});

// ── G4: the Monitor's groups ────────────────────────────────────────────────
const regs = [
    { name: '_G_COS_PHI[0]', unit: '', category: 'other', influxdb_measurement: 'power_factor' },
    { name: 'power_factor_total', unit: '', category: 'power', influxdb_measurement: 'power_factor' },
    { name: '_G_THD_ULN[0]', unit: '%', category: 'other', influxdb_measurement: 'thd' },
    { name: 'u_unbalance', unit: '%', category: 'other', influxdb_measurement: 'voltage' },
    { name: 'voltage_l1_n', unit: 'V', category: 'voltage', influxdb_measurement: 'voltage' },
    { name: 'motor_rpm', unit: 'rpm', category: 'motor', category_label: 'Motor', influxdb_measurement: '' },
    { name: 'odd_one', unit: '', category: 'other', influxdb_measurement: 'vendor_extra_block' },
    { name: 'x', unit: 'zz', category: 'other' },
    { name: 'calc', unit: 'W', influxdb_measurement: 'calculated', calculated: true },
];
guard('G4 on screen', () => {
  // what the value list shows
  for (const lang of ['en', 'ro']) {
    const a = app(lang);
    const box = { innerHTML: '', querySelectorAll: () => [] };
    ctx.document.getElementById = id => (id === 'monitorCategories' ? box : null);
    a.monitorData = {};
    a._monRegList = () => regs.filter(r => !r.calculated);
    a._monCalcRegs = () => regs.filter(r => r.calculated);
    try { a.renderMonitorCategories(); } finally { ctx.document.getElementById = () => null; }
    const heads = [...box.innerHTML.matchAll(/monitor-category-header[^>]*>\s*<span class="arrow">[^<]*<\/span>\s*<span>([^<]*)<\/span>/g)].map(m => m[1]);
    check(`G4 ${lang}: the list's headings are names, not keys`, heads.length >= 6 && heads.every(h => !/_/.test(h)), heads);
    check(`G4 ${lang}: "Power factor" in the page's language`, heads.includes(DICT[lang]['sec.power_factor']), heads);
    check(`G4 ${lang}: a heading works from the keyboard`, /monitor-category-header" role="button" tabindex="0" aria-expanded="true"/.test(box.innerHTML));
  }
});
guard('G4', () => {
  for (const lang of ['en', 'ro']) {
    const a = app(lang);
    const g = a._monGroups(regs);
    const names = g.map(x => x.name);
    check(`G4 ${lang}: no group shows a raw key`, names.every(s => !/_/.test(s) && s === s.trim() && /^\p{Lu}/u.test(s)), names);
    const pf = g.find(x => x.id === 'power_factor');
    check(`G4 ${lang}: "power_factor" is named from the dictionary`, pf && pf.name === DICT[lang]['sec.power_factor'], pf);
    check(`G4 ${lang}: a template's own label wins`, g.find(x => x.id === 'motor')?.name === 'Motor');
    check(`G4 ${lang}: a key with no name anywhere reads humanised`, g.find(x => x.id === 'vendor_extra_block')?.name === 'Vendor extra block');
    check(`G4 ${lang}: the server's category wins over the measurement`, g.find(x => x.id === 'power')?.regs.length === 1
          && g.find(x => x.id === 'voltage')?.regs.length === 2);
    check(`G4 ${lang}: "Other" comes last`, g[g.length - 1].id === 'other', g.map(x => x.id));
    check(`G4 ${lang}: calculated values have a name`, g.find(x => x.id === 'calculated')?.name === DICT[lang]['sec.calculated']);
  }
  const a = app('en');
  check('G4: one naming for every category list', a._catName('power_factor') === 'Power factor' && a._catName('power') === 'Power'
        && a._catName('x', 'Its label') === 'Its label' && a._catName('') === 'Other' && a._catName('foo_bar_baz') === 'Foo bar baz');
});

console.log(`${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
