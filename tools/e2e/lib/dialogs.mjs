// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// Answers the page's own dialogs (ui/js/app-dialogs.js) in an e2e — and any
// native confirm/prompt/alert that is left, which is counted so a test can
// say none fired.
//
//   const dlg = await watchDialogs(page);      // before page.goto
//   dlg.answer = true;                         // the default answer
//   dlg.answers.push(false, { max: 5 });       // the next answers, in order
//   dlg.seen     → [{kind, title, message, details, typed, fields, answer}]
//   dlg.results  → what each answer did: {open, errors} (open = refused, the
//                  dialog kept its error and was then cancelled)
//   dlg.native   → how many native dialogs fired
//   dlg.manual = true                          // leave dialogs open (drive the UI)
//
// An answer is what a user does: true / false for a confirm; for a dialog
// with fields an object of field values, a bare value for its first field,
// or null to cancel. The typed confirmation is typed for you on true.

export async function watchDialogs(page) {
  const w = { seen: [], results: [], native: 0, answers: [], answer: true, manual: false };
  const next = () => (w.answers.length ? w.answers.shift() : w.answer);

  page.on('dialog', d => {
    w.native++;
    const a = next();
    w.seen.push({ kind: 'native', title: '', message: d.message(), answer: a });
    if (a === false || a === null) d.dismiss().catch(() => {});
    else d.accept(typeof a === 'string' || typeof a === 'number' ? String(a) : undefined).catch(() => {});
  });

  await page.exposeFunction('__e2eDialog', detail => {
    if (w.manual) { w.seen.push({ ...detail, answer: undefined }); return { manual: true }; }
    const a = next();
    w.seen.push({ ...detail, answer: a });
    return { answer: a === undefined ? true : a };
  });
  await page.exposeFunction('__e2eDialogResult', r => { w.results.push(r); });

  await page.addInitScript(() => {
    document.addEventListener('mbg:dialog', async e => {
      const r = await window.__e2eDialog(e.detail);
      if (r.manual) return;
      await new Promise(res => setTimeout(res, 0));
      const out = await window.app._dialogAnswer(r.answer);
      window.__e2eDialogResult(out);
      // a refused answer leaves the dialog open with its error: record it,
      // then cancel, so the page under it is usable again
      if (out && out.open) await window.app._dialogAnswer(null);
    });
  });
  return w;
}

// the dialog currently open in the page, or null
export async function openDialog(page) {
  return page.evaluate(() => {
    const m = document.getElementById('appDialog');
    if (!m || !m.classList.contains('active')) return null;
    return {
      title: document.getElementById('appDialogTitle').textContent,
      message: document.getElementById('appDialogMsg').textContent,
      ok: document.getElementById('appDialogOk').textContent,
      okDisabled: document.getElementById('appDialogOk').disabled,
      error: document.getElementById('appDialogErr').textContent,
    };
  });
}
