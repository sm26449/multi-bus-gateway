/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// Delete and Restore (ADR-0032 §8, L3.11c) — augments JanitzaMonitor.prototype.
//
//   _undoToast({id, name, resets, fromPage})
//                         after a delete: a toast with Undo for 10 s. Undo is
//                         the restore; the toast says plainly what a restore
//                         sets back to safe instead of bringing back (the
//                         delete's `restore_resets`: an unlocked device comes
//                         back write-locked, bindings that were on come back
//                         off). Undo takes the focus the deleted item had; Esc
//                         dismisses; when it ends with the focus inside, the
//                         focus goes to the list's heading, never to <body>.
//   _restoreNow(id)       POST /api/devices/{id}/restore through the one write
//                         path (ADR-0030): {ok, reset, errors}
//   openRestoreDialog()   "Deleted devices: restore…" (the ⋯ menu's closing
//                         line, on the page and the list): every deleted
//                         device, each that cannot come back as it was saying
//                         why before the click (GET …/restorable?check=true:
//                         the restore's own checks), Restore / Forget per row.
Object.assign(JanitzaMonitor.prototype, {

    UNDO_MS: 10000,

    // what a restore resets, in words ('' when it brings the device back exactly)
    _resetsText(r) {
        const t = (k, d, p) => this.t(k, d, p);
        r = r || {};
        const parts = [];
        if (r.write_locked) parts.push(t('undo.resetLock', 'write-locked'));
        const cmds = r.commands || [];
        if (cmds.length) parts.push(this.tn('undo.resetCmds', cmds.length, 'with {n} command binding(s) off ({names})', { names: cmds.join(', ') }));
        const comps = r.components || [];
        if (comps.length) parts.push(this.tn('undo.resetComps', comps.length, 'with {n} component(s) write-locked ({names})', { names: comps.join(', ') }));
        return parts.join(t('undo.and', ' and '));
    },

    // what a restore starts again beside reading (review m6): its REST push,
    // to which host ('' when nothing)
    _resumesText(list, ownId) {
        const t = (k, d, p) => this.t(k, d, p);
        return (list || []).map(x => (x.device && x.device !== ownId
            ? t('undo.resumesOf', 'The REST push of {device} to {host} resumes with it.', { device: x.device, host: x.host || '—' })
            : t('undo.resumes', 'Its REST push to {host} resumes with it.', { host: x.host || '—' }))).join(' ');
    },

    // a toast with one action button (Open it): a short way, never a jump
    _actionToast(type, title, message, label, run, duration = 9000) {
        const container = document.getElementById('toast-container');
        if (!container) return;
        const el = document.createElement('div');
        el.className = `toast ${type}`;
        el.innerHTML = `<span class="toast-icon" aria-hidden="true">&#10003;</span>
            <div class="toast-content"><div class="toast-title">${this._esc(title)}</div>
                ${message ? `<div class="toast-message">${this._esc(message)}</div>` : ''}
                <div class="toast-actions"><button type="button" class="btn btn-secondary btn-sm" data-toast-act>${this._esc(label)}</button></div></div>
            <button type="button" class="toast-close" aria-label="${this._esc(this.t('common.dismiss', 'Dismiss'))}">&times;</button>`;
        const gone = () => { el.classList.add('fade-out'); setTimeout(() => el.remove(), 300); };
        el.querySelector('[data-toast-act]').addEventListener('click', () => { gone(); run(); });
        el.querySelector('.toast-close').addEventListener('click', gone);
        el.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); gone(); } });
        container.appendChild(el);
        setTimeout(() => { if (el.parentNode && !el.contains(document.activeElement)) gone(); }, duration);
        return el;
    },

    // a page left for the list: the route that follows (the address changed)
    // draws the list and puts the focus on its heading — waited for, so the
    // toast's Undo takes the focus once, after it, not before it
    async _routeSettled(maxMs = 1500) {
        await new Promise(r => setTimeout(r, 0));
        const until = Date.now() + maxMs;
        while (this._routeApplying && Date.now() < until) await new Promise(r => setTimeout(r, 30));
        await new Promise(r => setTimeout(r, 0));
    },

    // ── Undo in the delete's toast ───────────────────────────────────────
    _undoToast(o) {
        const t = (k, d, p) => this.t(k, d, p);
        const container = document.getElementById('toast-container');
        if (!container) return;
        // one Undo at a time: an older one ends as if its time were up
        container.querySelectorAll('.undo-toast').forEach(x => x._end && x._end(false));
        const name = o.name || o.id;
        // its settings could not be kept: nothing to undo, said (review m2)
        if (o.restorable === false) {
            this.showToast('warning', t('devices.deleted', 'Device deleted'),
                t('undo.notKept', '{name}: its settings could not be kept, so it cannot be restored.', { name }), 9000);
            return;
        }
        const resets = this._resetsText(o.resets);
        const resumes = this._resumesText(o.resumes, o.id);
        const el = document.createElement('div');
        el.className = 'toast success undo-toast';
        el.setAttribute('role', 'group');
        el.setAttribute('aria-labelledby', `undoT-${o.id}`);
        const msgId = `undoM-${o.id}`;
        el.innerHTML = `
            <span class="toast-icon" aria-hidden="true">&#10003;</span>
            <div class="toast-content">
                <div class="toast-title" id="undoT-${this._esc(o.id)}">${this._esc(t('devices.deleted', 'Device deleted'))}</div>
                <div class="toast-message" id="${this._esc(msgId)}">${this._esc(`${name} (${o.id})`)}${resets
                    ? `<br>${this._esc(t('undo.resetsSaid', 'Undo brings it back {what}.', { what: resets }))}` : ''}${resumes
                    ? `<br>${this._esc(resumes)}` : ''}</div>
                <div class="toast-actions">
                    <button type="button" class="btn btn-secondary btn-sm toast-undo" aria-describedby="${this._esc(msgId)}">
                        <i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> <span class="toast-undo-label">${this._esc(t('undo.undo', 'Undo'))}</span>
                        <span class="toast-undo-s" aria-hidden="true">${Math.round(this.UNDO_MS / 1000)}</span></button>
                </div>
                <div class="toast-undo-bar" aria-hidden="true"><span></span></div>
            </div>
            <button type="button" class="toast-close" aria-label="${this._esc(t('common.dismiss', 'Dismiss'))}">&times;</button>`;
        const undo = el.querySelector('.toast-undo');
        const secs = el.querySelector('.toast-undo-s');
        const bar = el.querySelector('.toast-undo-bar span');
        const until = Date.now() + this.UNDO_MS;
        let done = false;
        // where the focus goes when the toast leaves while holding it
        const home = () => {
            const h = document.querySelector('#devicesListView h2');
            if (h && h.offsetParent !== null) return h;
            return document.querySelector('#deviceDetailView h2, #deviceDetailView [tabindex="-1"]') || document.querySelector('main, [role="main"]');
        };
        const end = (focusHome = true) => {
            if (done) return;
            done = true;
            clearInterval(tick);
            const had = el.contains(document.activeElement);
            el.classList.add('fade-out');
            setTimeout(() => el.remove(), 300);
            if (had && focusHome) {
                const h = home();
                if (h) { if (!h.hasAttribute('tabindex')) h.setAttribute('tabindex', '-1'); h.focus(); }
            }
        };
        el._end = end;
        const tick = setInterval(() => {
            const left = until - Date.now();
            if (left <= 0) { end(); return; }
            secs.textContent = String(Math.ceil(left / 1000));
            if (bar) bar.style.width = `${Math.max(0, (left / this.UNDO_MS) * 100)}%`;
        }, 250);
        el.querySelector('.toast-close').addEventListener('click', () => end());
        el.addEventListener('keydown', e => {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end(); }
        });
        undo.addEventListener('click', async () => {
            if (done || undo.disabled) return;
            undo.disabled = true;
            undo.setAttribute('aria-busy', 'true');
            clearInterval(tick);
            const r = await this._restoreNow(o.id, o.deletedTs);
            done = true;
            el.remove();
            // back already (the dialog, another tab): said as back (review m2)
            if (!r.ok && r.code === 'exists') {
                await this._afterRestore(o.id, o.fromPage, t('undo.alreadyBackTitle', 'Already back'),
                    t('undo.alreadyBack', '{name} is already back.', { name }), 'info');
                return;
            }
            if (!r.ok) {
                this.showToast('error', t('undo.failed', 'Undo failed'),
                    t('undo.failedWhy', '{why} — it stays in "Deleted devices": ⋯ → Deleted devices: restore…', { why: r.errors.join('; ') }), 9000); // admin: Undo follows an admin's delete
                const h = home();
                if (h) { if (!h.hasAttribute('tabindex')) h.setAttribute('tabindex', '-1'); h.focus(); }
                return;
            }
            const said = this._resetsText(r.reset);
            const msg = [said ? t('undo.restoredReset', '{name}: back {what} — unlock or turn them on from its ⋯ when ready.', { name, what: said }) : name,
                         this._resumesText(r.resumed, o.id)].filter(Boolean).join(' ');
            await this._afterRestore(o.id, o.fromPage, t('devices.restored', 'Device restored'), msg, 'success');
        });
        container.appendChild(el);
        // the item that had the focus is gone (its row, its page): Undo has it.
        // The list drawn again and the route's heading may take it a moment
        // later: taken back once, unless the user has gone somewhere else
        undo.focus();
        const keep = () => {
            const a = document.activeElement;
            if (!done && (!a || a === document.body || a.matches('#devicesListView h2, #devicesPage, #deviceDetailView h2'))) undo.focus();
        };
        setTimeout(keep, 150);
        setTimeout(keep, 600);
    },

    // where the user still is: the list the delete left them on — the page it
    // was deleted from opens again, or its row has the focus. Gone elsewhere
    // meanwhile: they stay there, the toast offers "Open it" (review m4)
    async _afterRestore(id, toPage, title = '', message = '', type = 'success') {
        if (this._fetchDevices) await this._fetchDevices(true).catch(() => {});
        const onList = this.currentPage === 'devices' && this._view?.type !== 'device'
            && document.getElementById('devicesListView')?.offsetParent !== null;
        if (!onList) {
            if (title) this._actionToast(type, title, message, this.t('restore.open', 'Open it'), () => this.openDeviceDetail(id));
            return;
        }
        if (title) this.showToast(type, title, message, 7000);
        if (toPage && this.openDeviceDetail) {
            await this.openDeviceDetail(id);
            const h = document.querySelector('#deviceDetailView h2');
            if (h) { if (!h.hasAttribute('tabindex')) h.setAttribute('tabindex', '-1'); h.focus(); }
            return;
        }
        if (this.currentPage === 'devices' && this._view?.type !== 'device' && this.renderDevicesList) {
            await this.renderDevicesList();
            const row = document.querySelector(`#devicesList .device-row[data-f="${CSS.escape(id)}"] [data-dev-menu]`);
            if (row) row.focus();
        }
    },

    // deletedTs (an Undo): the delete it undoes — a later delete's kept
    // settings are refused (409 tombstone_changed, review m1)
    async _restoreNow(id, deletedTs) {
        let rsp, d;
        const q = deletedTs !== undefined && deletedTs !== null ? `?deleted_ts=${encodeURIComponent(deletedTs)}` : '';
        try {
            rsp = await window.DeviceStore.devWrite(id, 'POST', `/api/devices/${encodeURIComponent(id)}/restore${q}`);
            d = await rsp.json().catch(() => ({}));
        } catch (e) {
            return { ok: false, reset: {}, errors: [this._errMsg(e)] };
        }
        if (rsp.ok) return { ok: true, reset: d.reset || {}, resumed: d.resumed || [], device: d.device || null, errors: [] };
        const det = d && d.detail;
        const errors = det && Array.isArray(det.errors) ? det.errors.map(String) : [this._respError(rsp, d)];
        return { ok: false, reset: {}, errors, status: rsp.status, code: (det && det.code) || '' };
    },

    // ── the Restore dialog ───────────────────────────────────────────────
    async openRestoreDialog() {
        const modal = document.getElementById('restoreModal');
        if (!modal) return;
        // the focus goes back to what opened it: a menu item is gone once its
        // menu closes, so its ⋯ button
        let opener = document.activeElement;
        const menu = opener && opener.closest ? opener.closest('.mbg-menu') : null;
        if (menu && menu.id) opener = document.querySelector(`[aria-controls="${CSS.escape(menu.id)}"]`) || opener;
        const list = document.getElementById('restoreList');
        list.innerHTML = `<p class="field-hint" role="status"><span class="btn-spinner" aria-hidden="true"></span> ${this._esc(this.t('restore.loading', 'Asking the gateway what can come back…'))}</p>`;
        this.openModal('restoreModal', { focus: modal.querySelector('.modal-close') });
        // a restore draws the list again: the ⋯ it was opened from is then a
        // new element, found again by what it is
        const sel = opener && opener.dataset && opener.dataset.devMenu
            ? `[data-dev-menu="${CSS.escape(opener.dataset.devMenu)}"][aria-controls="${CSS.escape(opener.getAttribute('aria-controls') || '')}"]` : '';
        modal._returnFocus = opener ? { focus: () => {
            const el = opener.isConnected ? opener : (sel ? document.querySelector(sel) : null);
            if (el) el.focus();
        } } : null;
        await this._restoreLoad();
    },

    async _restoreLoad() {
        const list = document.getElementById('restoreList');
        if (!list) return;
        const t = (k, d, p) => this.t(k, d, p);
        let items;
        try {
            const r = await fetch('/api/devices/restorable?check=true');
            if (!r.ok) throw new Error(this._respError(r, await r.json().catch(() => ({}))));
            items = (await r.json()).devices || [];
        } catch (e) {
            list.innerHTML = `<p class="text-danger" role="alert">${this._esc(t('devices.restorableLoadFail', 'Could not load restorable devices.'))} ${this._esc(this._errMsg(e))}</p>`;
            return;
        }
        this._restoreItems = items;
        list.innerHTML = items.length
            ? `<ul class="restore-list" aria-label="${this._esc(t('devices.restorable', 'Deleted devices (restorable)'))}">${items.map(d => this._restoreRowHtml(d)).join('')}</ul>`
            : `<p class="field-hint">${this._esc(t('ddv.noRestorable', 'No deleted device to restore.'))}</p>`;
        list.querySelectorAll('[data-restore-id]').forEach(b => b.addEventListener('click', () => this._restoreFromDialog(b.dataset.restoreId, b)));
        list.querySelectorAll('[data-forget-id]').forEach(b => b.addEventListener('click', () => this._forgetFromDialog(b.dataset.forgetId)));
        list.querySelectorAll('[data-restore-open]').forEach(b => b.addEventListener('click', () => this._restoreOpen(b.dataset.restoreOpen)));
    },

    _restoreRowHtml(d, state = {}) {
        const t = (k, dd, p) => this.t(k, dd, p);
        const id = String(d.id);
        const sid = this._esc(id.replace(/[^A-Za-z0-9_-]/g, '_'));
        const when = d.deleted_ts ? new Date(d.deleted_ts * 1000).toLocaleString(this._loc ? this._loc() : undefined) : '';
        const problems = d.problems || [];
        const held = problems.length > 0;
        const resets = this._resetsText(d.resets);
        const resumes = this._resumesText(d.resumes, id);
        const sub = [this._protoLabel ? this._protoLabel(d.protocol || '') : (d.protocol || ''), d.template || '—',
                     this.tn('restore.measurements', d.registers || 0, '{n} measurement(s)'),
                     when ? t('restore.deletedOn', 'deleted {when}', { when }) : ''].filter(Boolean).join(' · ');
        const why = held ? `<div class="restore-problem" id="restoreWhy-${sid}">
                <b>${this._esc(t('restore.cannot', 'Cannot be restored as it was:'))}</b>
                <ul>${problems.map(p => `<li>${this._esc(p)}</li>`).join('')}</ul>
                <span class="field-hint">${this._esc(t('restore.cannotFix', 'Fix what it names (or delete what took its place), then open this list again.'))}</span></div>` : '';
        const noteText = [resets ? t('restore.comesBack', 'Comes back {what}.', { what: resets }) : '', resumes].filter(Boolean).join(' ');
        const note = !held && noteText ? `<p class="field-hint restore-note" id="restoreNote-${sid}">${this._esc(noteText)}</p>` : '';
        const done = state.restored ? `<p class="restore-done" role="status"><i aria-hidden="true" class="bi bi-check-circle"></i> ${this._esc(t('restore.done', 'Restored'))}${state.said ? ` — ${this._esc(state.said)}` : ''}
                <button type="button" class="btn btn-ghost btn-sm" data-restore-open="${this._esc(id)}">${this._esc(t('restore.open', 'Open it'))}</button></p>` : '';
        const err = state.error ? `<p class="text-danger restore-err" role="alert" id="restoreErr-${sid}">${this._esc(state.error)}</p>` : '';
        const desc = [held ? `restoreWhy-${sid}` : '', note ? `restoreNote-${sid}` : '', err ? `restoreErr-${sid}` : ''].filter(Boolean).join(' ');
        return `<li class="restore-row" data-restore-row="${this._esc(id)}">
            <div class="restore-main">
                <div class="restore-title"><b>${this._esc(d.name || id)}</b> <span class="dev-chip">${this._esc(id)}</span></div>
                <div class="field-hint">${this._esc(sub)}</div>
                ${why}${note}${err}${done}
            </div>
            ${state.restored ? '' : `<div class="restore-acts">
                <button type="button" class="btn btn-primary btn-sm" data-restore-id="${this._esc(id)}"
                    ${held ? 'aria-disabled="true"' : ''} ${desc ? `aria-describedby="${desc}"` : ''}>
                    <i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> ${this._esc(t('devices.restore', 'Restore'))}<span class="sr-only"> ${this._esc(d.name || id)}</span></button>
                <button type="button" class="btn btn-ghost btn-sm" data-forget-id="${this._esc(id)}" ${this._ib(t('devices.forget', 'Forget'), d.name || id)}>
                    <i aria-hidden="true" class="bi bi-trash"></i></button>
            </div>`}
        </li>`;
    },

    _restoreRowDraw(id, state) {
        const d = (this._restoreItems || []).find(x => x.id === id);
        const row = document.querySelector(`#restoreList [data-restore-row="${CSS.escape(id)}"]`);
        if (!d || !row) return null;
        row.outerHTML = this._restoreRowHtml(d, state);
        const fresh = document.querySelector(`#restoreList [data-restore-row="${CSS.escape(id)}"]`);
        fresh.querySelectorAll('[data-restore-id]').forEach(b => b.addEventListener('click', () => this._restoreFromDialog(b.dataset.restoreId, b)));
        fresh.querySelectorAll('[data-forget-id]').forEach(b => b.addEventListener('click', () => this._forgetFromDialog(b.dataset.forgetId)));
        fresh.querySelectorAll('[data-restore-open]').forEach(b => b.addEventListener('click', () => this._restoreOpen(b.dataset.restoreOpen)));
        return fresh;
    },

    async _restoreFromDialog(id, btn) {
        // held: it says why (the reason is read with the button), nothing sent
        if (btn && btn.getAttribute('aria-disabled') === 'true') {
            const why = document.getElementById(btn.getAttribute('aria-describedby')?.split(' ')[0] || '');
            if (why) { why.setAttribute('tabindex', '-1'); why.focus(); }
            return;
        }
        if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
        // the row's own kept settings: a later delete at the same id is not
        // restored in its place (second review R3)
        const row0 = (this._restoreItems || []).find(x => x.id === id) || {};
        const r = await this._restoreNow(id, row0.deleted_ts);
        let row;
        if (!r.ok && r.code === 'exists') {
            row = this._restoreRowDraw(id, { restored: true, said: this.t('undo.alreadyBackShort', 'already back') });
            row?.querySelector('[data-restore-open]')?.focus();
            return;
        }
        if (r.ok) {
            row = this._restoreRowDraw(id, { restored: true, said: this._resetsText(r.reset)
                ? this.t('restore.comesBackDone', 'back {what}', { what: this._resetsText(r.reset) }) : '' });
            if (this.currentPage === 'devices' && this._view?.type !== 'device' && this.renderDevicesList) this.renderDevicesList();
            else if (this._fetchDevices) this._fetchDevices(true).catch(() => {});
            row?.querySelector('[data-restore-open]')?.focus();
        } else {
            row = this._restoreRowDraw(id, { error: this.t('restore.refused', 'Not restored: {why}', { why: r.errors.join('; ') }) });
            row?.querySelector('[data-restore-id]')?.focus();
        }
    },

    async _forgetFromDialog(id) {
        if (!await this.confirmAction({
            title: this.t('devices.forgetTitle', 'Forget {id}', { id }),
            message: this.t('devices.forgetConfirm', 'Its kept settings are deleted for good: it can no longer be restored.'),
            confirmLabel: this.t('devices.forget', 'Forget'), danger: true,
        })) return;
        const rsp = await fetch(`/api/devices/restorable/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (!rsp.ok) {
            const d = await rsp.json().catch(() => ({}));
            this.showToast('error', this.t('devices.forget', 'Forget'), this._respError(rsp, d));
            return;
        }
        this.showToast('success', this.t('devices.forgotten', 'Settings forgotten'), id);
        await this._restoreLoad();
        if (this._renderRestorableDevices) this._renderRestorableDevices();
        document.querySelector('#restoreList [data-restore-id], #restoreModal .modal-close')?.focus();
    },

    async _restoreOpen(id) {
        const modal = document.getElementById('restoreModal');
        if (modal) modal._returnFocus = null;
        this.closeModal('restoreModal');
        if (this.openDeviceDetail) await this.openDeviceDetail(id);
    },
});
