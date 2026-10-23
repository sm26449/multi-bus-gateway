/* The OPC UA tab in Settings: off by default and said so, enabled with a
 * port and encryption, the status line names the endpoint and what it
 * serves, the Romanian page. A real OPC UA client then reads through it
 * (tools/e2e/opcua_client_check.py, run by the caller).
 *
 *   docker run -d --rm --name mbg-opcua-test -p 127.0.0.1:18090:8080 -p 127.0.0.1:14840:4840 \
 *     -e UI_HOST=0.0.0.0 --entrypoint python -v "$PWD":/app:ro -v <cfg>:/app/config -w /app <image with asyncua> main.py
 *   MBG_URL=http://127.0.0.1:18090 MBG_USER=admin MBG_PASS=<pass> CHROMIUM_PATH=<chrome> node opcua_e2e.mjs
 *
 * Config it expects: login on (the OPC UA users are the gateway's).
 */
import { chromium } from 'playwright';
const BASE = process.env.MBG_URL || 'http://127.0.0.1:18090';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond }); console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + JSON.stringify(extra) : ''}`); };
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(20000);
const errs = []; page.on('console', m => { if (m.type() === 'error' && !/40[1349]|422/.test(m.text())) errs.push(m.text()); }); page.on('pageerror', e => errs.push(String(e)));
try {
  const login = await ctx.request.post(BASE + '/api/auth/login', { data: { username: process.env.MBG_USER || 'admin', password: process.env.MBG_PASS } });
  check('logged in', login.ok());
  await page.goto(BASE + '/'); await page.waitForLoadState('networkidle');
  const later = page.locator('button:has-text("Later")'); if (await later.count()) await later.first().click().catch(() => {});
  await page.click('[data-page="config"]'); await page.waitForTimeout(800);
  await page.click('[data-cfgtab="opcua"]'); await page.waitForTimeout(1200);
  const panel = page.locator('[data-cfgpanel="opcua"]');
  check('the OPC UA tab says what it is for and that it is off', /SCADA systems and HMI panels/.test(await panel.innerText())
        && /Off\./.test(await page.locator('#opcuaStatus').innerText()), await page.locator('#opcuaStatus').innerText());
  check('encryption defaults to required', await page.locator('#cfgOpcUaEncryption').inputValue() === 'required');
  check('every setting is labelled', (await panel.locator('label[for="cfgOpcUaPort"], label[for="cfgOpcUaHost"], label[for="cfgOpcUaEncryption"], label[for="cfgOpcUaUpdate"]').count()) === 4);
  await panel.locator('label.toggle-switch .toggle-slider').click();      // the switch, as a person clicks it
  check('the switch turns on', await page.locator('#cfgOpcUaEnabled').isChecked());
  await page.locator('#cfgOpcUaPort').fill('70000');
  await panel.locator('button[data-action="saveOpcUa"]').click(); await page.waitForTimeout(1000);
  check('a wrong port is refused in words', /port: 1\.\.65535/.test(await page.locator('#opcuaSaveFeedback').innerText()));
  await page.locator('#cfgOpcUaPort').fill('4840');
  await page.locator('#cfgOpcUaEncryption').selectOption('optional');
  await panel.locator('button[data-action="saveOpcUa"]').click();
  let st = '';
  for (let i = 0; i < 20 && !/Running/.test(st); i++) { await page.waitForTimeout(500); st = await page.locator('#opcuaStatus').innerText(); }
  check('enabled: the status names the endpoint and what it serves', /● Running — opc\.tcp:\/\/0\.0\.0\.0:4840\/mbg · \d+ devices, \d+ values, \d+ commands · the gateway accounts/.test(st), st);
  const cert = await ctx.request.get(BASE + '/api/opcua/certificate');
  check('the server certificate downloads', cert.ok() && (await cert.body())[0] === 0x30);
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'ro'); } catch (e) {} });
  await page.reload(); await page.waitForLoadState('networkidle');
  await page.click('[data-page="config"]'); await page.waitForTimeout(800);
  await page.click('[data-cfgtab="opcua"]'); await page.waitForTimeout(1200);
  const ro = await panel.innerText();
  check('in Romanian', /Server OPC UA/.test(ro) && /Rulează/.test(ro) && /conturile gateway-ului/.test(ro) && !/opcua\./.test(ro), ro.slice(0, 300));
  await page.evaluate(() => { try { localStorage.setItem('mbg-lang', 'en'); } catch (e) {} });
  check('no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
} catch (e) { check('script ran to the end', false, String(e).slice(0, 300)); }
finally { await browser.close(); const f = results.filter(r => !r.ok).length; console.log(`\n${results.length - f}/${results.length} passed`); process.exit(f ? 1 : 0); }
