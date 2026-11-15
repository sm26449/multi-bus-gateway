// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The five opt-in tuning keys (ADR-0012) in the steps-rule editor, against a
// REAL throwaway gateway (nothing mocked): resume_on_boot, step_down_margin,
// timing.boot_wait_s, timing.urgent_retries, timing.step_down_hold_s.
//   - a rule shaped like production ov-u1..u4 (all five keys) opens with the
//     "Advanced / tuning" section open and every box filled; an untouched
//     Save keeps all five exactly (rules.yaml via GET /api/rules raw)
//   - edits are saved, a reload shows them; an emptied box removes its key,
//     a typed 0 is kept as 0, a key never set is never written
//   - an out-of-range value is refused by the server and said next to its box
//   - a non-steps kind hides the section
// Screenshots to $OUT (default the scratch dir of the run).
//
//   tools/e2e/fixture_up.sh safety 18087
//   CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 node tools/e2e/rules_tuning_e2e.mjs
import { mkdirSync } from 'node:fs';
import { chromium } from 'playwright';
import { watchDialogs } from './lib/dialogs.mjs';

const BASE = process.env.MBG_URL || 'http://127.0.0.1:18087';
const OUT = process.env.OUT || process.env.SHOTS_DIR || '/tmp/mbg-e2e-rules-tuning';
mkdirSync(OUT, { recursive: true });
const RID = 'tune-e2e';
const results = [];
const check = (name, cond, extra = '') => {
  results.push({ name, ok: !!cond });
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + extra : ''}`);
};
const RULE = {
  id: RID, label: 'Tuning e2e', kind: 'steps', mode: 'shadow',
  target: { device: 'sunfield-u1', command: 'power_limit' },
  signal: 'max(sunfield-u1.voltage_l1_n, sunfield-u1.voltage_l2_n)',
  steps: [{ at: 250, value: 80, label: 'Warning' }, { at: 253, value: 50, label: 'Emergency', fast: true }],
  release_below: 248, normal: { value: 100 }, params: { revert_s: 0 },
  resume_on_boot: true, step_down_margin: 0.5,
  timing: { every_s: 2, debounce: 3, min_interval_s: 30, reassert_s: 120,
            boot_wait_s: 60, step_down_hold_s: 90, urgent_retries: 3 },
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.setDefaultTimeout(15000);
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
// a 404 is the setup's DELETE of a rule not there yet, a 422 the refusal under
// test, "Failed to fetch" a request the reload aborted
page.on('console', m => {
  if (m.type() === 'error' && !/40[349]|422|Failed to fetch/.test(m.text())) errs.push(m.text());
});
const dlg = await watchDialogs(page);
const api = (method, path, body) => page.evaluate(async ([method, path, body]) => {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}, [method, path, body]);
const stored = async () => ((await api('GET', '/api/rules')).json.rules || []).find(r => r.id === RID)?.raw;
const puts = [];
page.on('request', r => { if (r.method() === 'PUT' && r.url().endsWith(`/api/rules/${RID}`)) puts.push(r.postDataJSON()); });

async function openRules() {
  await page.goto(BASE + '/');
  await page.waitForLoadState('load');
  const later = page.locator('button:has-text("Later")');
  if (await later.count()) await later.first().click().catch(() => {});
  await page.evaluate(() => window.app.navigateTo('rules'));
  await page.waitForSelector(`[data-rule="${RID}"]`);
}
async function openEditor() {
  await page.evaluate(id => window.app.openRuleModal(id), RID);
  await page.waitForSelector('#rlStepDownMargin', { state: 'attached' });
}
const val = id => page.inputValue('#' + id);
const save = async () => { await page.click('[data-rule-save]'); await page.waitForTimeout(1200); };
const modalOpen = () => page.locator('#ruleModal .modal-close').isVisible();

try {
  await page.goto(BASE + '/');
  await api('DELETE', `/api/rules/${RID}`);
  const made = await api('POST', '/api/rules', RULE);
  check('setup: the production-shaped rule is created', made.status === 200, JSON.stringify(made.json).slice(0, 300));
  await openRules();

  // ── it opens with what is stored ─────────────────────────────────────
  await openEditor();
  const det = page.locator('[data-rl-tuning]');
  check('editor: the Advanced / tuning section is open (keys are set)', await det.evaluate(d => d.open));
  check('editor: all five boxes hold the stored values',
        (await val('rlBootWait')) === '60' && (await val('rlUrgentRetries')) === '3'
        && (await val('rlStepDownHold')) === '90' && (await val('rlStepDownMargin')) === '0.5'
        && await page.isChecked('#rlResumeOnBoot'),
        [await val('rlBootWait'), await val('rlUrgentRetries'), await val('rlStepDownHold'), await val('rlStepDownMargin')].join(','));
  check('editor: every tuning field is labelled and described', await page.evaluate(() =>
    ['rlBootWait', 'rlUrgentRetries', 'rlStepDownHold', 'rlStepDownMargin', 'rlResumeOnBoot'].every(id => {
      const el = document.getElementById(id);
      const labelled = document.querySelector(`label[for="${id}"]`) || el.closest('label');
      const hint = (el.getAttribute('aria-describedby') || '').split(/\s+/).some(h => document.getElementById(h));
      return labelled && hint;
    })));
  check('editor: the margin says its bound (highest step − release = 5)',
        (await page.getAttribute('#rlStepDownMargin', 'max')) === '5'
        && /5/.test(await page.locator('#rlStepDownMarginHint').innerText()));
  await page.locator('[data-rl-tuning]').scrollIntoViewIfNeeded();
  await page.locator('#ruleModal .modal-content').screenshot({ path: `${OUT}/rules_tuning_editor.png` }).catch(() => {});

  // an untouched Save keeps every key exactly
  dlg.answer = true;
  await save();
  const same = await stored();
  check('untouched save: all five keys kept exactly',
        same?.resume_on_boot === true && same?.step_down_margin === 0.5 && same?.timing?.boot_wait_s === 60
        && same?.timing?.urgent_retries === 3 && same?.timing?.step_down_hold_s === 90, JSON.stringify(same));
  check('untouched save: nothing sent (no change)', puts.length === 0, JSON.stringify(puts.at(-1)));

  // ── edit, save, reload, see ──────────────────────────────────────────
  if (await modalOpen()) await page.locator('#ruleModal .modal-close').click().catch(() => {});
  await openEditor();
  await page.fill('#rlStepDownMargin', '1.5');
  await page.fill('#rlStepDownHold', '120');
  await page.fill('#rlUrgentRetries', '');       // emptied: the key goes
  await page.fill('#rlBootWait', '0');           // typed 0: kept as 0
  await page.uncheck('#rlResumeOnBoot');
  await save();
  const sent = puts.at(-1) || {};
  check('save: what was typed is sent, the emptied box removes its key',
        sent.step_down_margin === 1.5 && sent.timing?.step_down_hold_s === 120 && sent.timing?.boot_wait_s === 0
        && !('urgent_retries' in (sent.timing || {})) && !('resume_on_boot' in sent), JSON.stringify(sent));
  const after = await stored();
  check('save: stored as typed',
        after?.step_down_margin === 1.5 && after?.timing?.step_down_hold_s === 120 && after?.timing?.boot_wait_s === 0
        && !('urgent_retries' in (after?.timing || {})) && !('resume_on_boot' in (after || {})), JSON.stringify(after));
  await page.reload();
  await openRules();
  const card = await page.locator(`[data-rule="${RID}"]`).innerText();
  check('card: the margin and the hold are said', /1\.5/.test(card) && /120 s/.test(card), card.slice(0, 500));
  await openEditor();
  check('after reload: the editor shows the saved values',
        (await val('rlStepDownMargin')) === '1.5' && (await val('rlStepDownHold')) === '120'
        && (await val('rlBootWait')) === '0' && (await val('rlUrgentRetries')) === ''
        && !(await page.isChecked('#rlResumeOnBoot')));

  // ── refused by the server, said next to the box ──────────────────────
  await page.fill('#rlStepDownMargin', '9');
  await page.fill('#rlStepDownHold', '4000');
  await save();
  const mErr = (await page.locator('#rlStepDownMarginErr').innerText().catch(() => '')).trim();
  const hErr = (await page.locator('#rlStepDownHoldErr').innerText().catch(() => '')).trim();
  check('out of range: the margin refusal is said under its box', /0 to 5/.test(mErr), mErr);
  check('out of range: the hold refusal is said under its box', /0 to 3600/.test(hErr), hErr);
  check('out of range: the boxes are marked invalid',
        (await page.getAttribute('#rlStepDownMargin', 'aria-invalid')) === 'true'
        && (await page.getAttribute('#rlStepDownHold', 'aria-invalid')) === 'true');
  check('out of range: nothing stored', (await stored())?.step_down_margin === 1.5);
  await page.locator('[data-rl-tuning]').scrollIntoViewIfNeeded();
  await page.locator('#ruleModal .modal-content').screenshot({ path: `${OUT}/rules_tuning_refused.png` }).catch(() => {});

  // ── another kind: the section does not apply ─────────────────────────
  await page.selectOption('#rlKind', 'condition');
  check('a condition rule: the tuning section is hidden', !(await page.locator('[data-rl-tuning]').isVisible()));
  await page.selectOption('#rlKind', 'steps');
  check('back to steps: the section is shown again', await page.locator('[data-rl-tuning]').isVisible());
  await page.locator('#ruleModal .modal-close').click().catch(() => {});

  // ── a rule that never had the keys: a save writes none of them ───────
  const plain = { ...RULE, id: RID, timing: { every_s: 2, debounce: 3, min_interval_s: 30, reassert_s: 120 } };
  delete plain.resume_on_boot; delete plain.step_down_margin;
  await api('DELETE', `/api/rules/${RID}`);
  await api('POST', '/api/rules', plain);
  await openRules();
  await openEditor();
  check('no keys: the section starts closed', !(await page.locator('[data-rl-tuning]').evaluate(d => d.open)));
  await page.fill('#rlLabel', 'Tuning e2e (renamed)');
  await save();
  const p = await stored();
  check('no keys: a save of another field writes none of the five',
        p?.label === 'Tuning e2e (renamed)' && !['resume_on_boot', 'step_down_margin'].some(k => k in p)
        && !['boot_wait_s', 'urgent_retries', 'step_down_hold_s'].some(k => k in (p.timing || {})), JSON.stringify(p));
  check('no page errors', errs.length === 0, errs.join(' | '));
  check('no native dialog', dlg.native === 0);
} catch (e) {
  check('ran to the end', false, String(e && e.stack || e));
} finally {
  await api('DELETE', `/api/rules/${RID}`).catch(() => {});
  await browser.close();
}
const failed = results.filter(r => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed${failed ? '' : ` · screenshots in ${OUT}`}`);
process.exit(failed ? 1 : 0);
