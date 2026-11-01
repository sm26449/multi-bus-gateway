// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// Add Device as a script drives it (ADR-0031 §2, L3.9): the one flow every
// way in opens — What is it · Connection · Check · Name & save. The next
// change to the flow changes this file, not every script.
//
//   await openAdd(page)                    the Devices page's Add button
//   await pickModel(page, query, tplId)    search, pick the card, Next
//   await answer(page, name, value)        a connection question: PortKind,
//                                          Reach, Speaks, Role (its radio)
//   await fill(page, field, value)         a connection field by its suffix:
//                                          Unit, Host, Port, Broker, MqttTopic…
//   await next(page)                       Next (or Create), waits for the step
//   await step(page)                       the step shown: what · connection ·
//                                          check · save · meters
//   await waitCheck(page)                  the Check step's answer (or refusal)
//   await create(page, {name})             Name & save: the name, then Create;
//                                          the result panel's state line
//   await addDevice(page, o)               the whole way for one device:
//                                          {query, template, conn: async page => …,
//                                           name, check: true|false}
export const PX = 'csAdd';

export async function openAdd(page) {
  const btn = page.locator('#devicesPage [data-action="openDeviceWizard"]').first();
  // the Devices list's button; from another view (a device page) its action itself
  if (await btn.isVisible()) await btn.click();
  else await page.evaluate(() => window.app.openDeviceWizard());
  await page.waitForSelector('#addDeviceModal.active');
  await page.waitForFunction(() => !document.querySelector('#addDevMain[aria-busy="true"]'));
}

export async function step(page) {
  return page.evaluate(() => document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep || '');
}

export async function next(page) {
  const was = await step(page);
  await page.click('#addDevNext');
  await page.waitForFunction(w => {
    const cur = document.querySelector('#addDevRail [aria-current="step"]')?.dataset.shellStep;
    const busy = document.querySelector('#addDevNext[aria-busy="true"]');
    const err = document.querySelector('#addDevErrors .dev-shell-errbox');
    const res = document.getElementById('adResult');
    return !busy && (cur !== w || err || res);
  }, was, { timeout: 20000 }).catch(() => {});
  return step(page);
}

export async function pickModel(page, query, tplId) {
  await page.fill('#adQ', query);
  await page.click(`#adResults [data-ad-tpl="${tplId}"]`);
  const st = await next(page);
  if (st === 'connection') await settled(page);
  return st;
}

// the Connection step reads the buses afresh when it is drawn: its answers
// are typed once that read is in (as a user does, a moment later)
export async function settled(page) {
  await page.waitForFunction(() => window.app?._csCache && !window.app._csCache.loading && window.app._csCache.at, null, { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(250);
}

export async function answer(page, name, value) {
  const id = `${PX}${name}-${String(value).replace(/[^A-Za-z0-9_-]/g, '-')}`;
  await page.waitForSelector(`#${id}`, { state: 'attached' });
  // a question the model answers alone is a fact (its one port): nothing to click
  if (await page.isChecked(`#${id}`) && !(await page.locator(`label:has(#${id})`).count())) return;
  await page.locator(`label:has(#${id})`).click();
  await page.waitForTimeout(150);
}

export async function fill(page, field, value) {
  const sel = `#${PX}${field}`;
  await page.waitForSelector(sel);
  await page.fill(sel, String(value));
  await page.locator(sel).blur();
  await page.waitForTimeout(150);
}

export async function waitCheck(page, timeout = 30000) {
  const ended = await page.waitForFunction(() => {
    const host = document.getElementById('adCheck');
    if (!host) return false;
    if (host.querySelector('.pv-refused')) return true;
    const st = host.querySelector('.pv-status');
    return !!st && !st.classList.contains('is-running');
  }, null, { timeout }).then(() => true, () => false);
  if (!ended) {
    return { state: 'timeout', refused: false, values: [], comps: [], step: await step(page),
             errors: await page.locator('#addDevErrors').innerText().catch(() => ''),
             check: await page.locator('#adCheck').innerText().catch(() => '') };
  }
  return page.evaluate(() => {
    const host = document.getElementById('adCheck');
    const st = host.querySelector('.pv-status');
    return {
      state: st ? [...st.classList].find(c => c.startsWith('is-'))?.slice(3) : 'refused',
      refused: !!host.querySelector('.pv-refused'),
      refusal: host.querySelector('.pv-refused')?.textContent.replace(/\s+/g, ' ').trim() || '',
      values: [...host.querySelectorAll('[data-pv-row]')].map(r => ({ name: r.dataset.pvRow, q: r.dataset.q,
        value: r.querySelector('.pv-val')?.textContent.trim() })),
      heard: host.querySelector('.pv-heard')?.textContent.trim() || '',
      comps: [...host.querySelectorAll('#adComps input[type="checkbox"]')].map(b => b.checked),
      next: document.getElementById('addDevNext').textContent.trim(),
    };
  });
}

export async function create(page, o = {}) {
  if (o.name !== undefined) {
    await page.fill('#adName', o.name);
    await page.locator('#adName').dispatchEvent('input');
  }
  // the gateway asked about it (the review strip, or its refusal)
  await page.waitForFunction(() => !document.querySelector('#adReview[aria-busy="true"]')
    && (document.querySelector('#adReviewBody .ad-strip') || document.getElementById('adRefuse')?.textContent), null, { timeout: 15000 });
  await page.waitForTimeout(500);
  await page.click('#addDevNext');
  await page.waitForSelector('#adResult', { timeout: 20000 });
  return page.locator('#adResultState').textContent();
}

export async function addDevice(page, o) {
  await openAdd(page);
  await pickModel(page, o.query, o.template);
  if (o.conn) await o.conn(page);
  await next(page);                       // → Check
  if (o.check !== false) await waitCheck(page);
  await next(page);                       // → Name & save
  return create(page, { name: o.name });
}
