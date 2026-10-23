// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
//
// dialogs — augments JanitzaMonitor.prototype
//
// One way to ask: confirm an action, or ask for a value, in the page's own
// modal instead of the browser's confirm()/prompt()/alert(). The native ones
// put "Yes:/No:" in the text because their buttons say OK/Cancel, sent a
// typed "abc" on as null, and cannot be styled, translated or tested.
//
//   confirmAction({title, message, details, confirmLabel, cancelLabel,
//                  danger, typed, submit})                → Promise<boolean>
//   askValues({title, message, details, confirmLabel, danger,
//              fields: [{name, label, kind, min, max, step, unit, initial,
//                        required, placeholder, hint, validate,
//                        options}], submit})
//     (kind: 'number' | 'text' | 'password' | 'select' — a select takes
//      options [{value, label}]; an empty value reads as null)
//                                                         → Promise<object|null>
//   askValue({title, message, …one field's props, submit}) → Promise<value|null>
//
// submit (optional) runs on OK with the button busy; a returned string is an
// error shown in the dialog, which stays open. One dialog at a time — a second
// call while one is open answers "no" at once. The API-key prompt has its own
// dialog (#appKeyDialog) because it is asked from inside a request, possibly
// one a dialog's submit started.

Object.assign(JanitzaMonitor.prototype, {

    confirmAction(o = {}) {
        return this._dialogOpen('confirm', o);
    },

    askValues(o = {}) {
        return this._dialogOpen('values', o);
    },

    askValue(o = {}) {
        const { title, message, details, confirmLabel, cancelLabel, danger, typed, submit, ...field } = o;
        return this.askValues({
            title, message, details, confirmLabel, cancelLabel, danger, typed,
            fields: [{ ...field, name: 'value' }],
            submit: submit ? (v => submit(v.value)) : null,
        }).then(v => (v ? v.value : null));
    },

    // a failed operation, said in a toast (a 403 for the account's role is
    // already explained by the fetch wrapper — see _roleRefused)
    _opError(title, e) {
        if (e && e.status === 403 && this._roleSpeaks(e.detail)) return;
        this.showToast('error', title, (e && e.message) || String(e || ''));
    },

    // the text of a refused request, for a submit to return: the server's
    // {detail: {errors: [...]}} or {detail: "…"}, else the status
    _respError(r, d) {
        const det = d && d.detail;
        if (det && Array.isArray(det.errors)) return det.errors.join('; ');
        if (typeof det === 'string' && det) return det;
        return det ? this._errMsg(d) : `HTTP ${r.status}`;
    },

    _dialogOpen(kind, o) {
        const cancelled = kind === 'confirm' ? false : null;
        if (this._dlg) {
            console.warn('a dialog is already open:', this._dlg.o.title);
            return Promise.resolve(cancelled);
        }
        const $ = id => document.getElementById(id);
        const modal = $('appDialog');
        if (!modal) return Promise.resolve(cancelled);
        return new Promise(resolve => {
            const fields = (kind === 'values' ? (o.fields || []) : []).map(f => ({ kind: 'number', required: true, ...f }));
            if (o.typed) fields.push({ name: '__typed', kind: 'text', required: true, typed: String(o.typed),
                label: this.t('dialog.typeToConfirm', 'Type {text} to confirm', { text: o.typed }) });
            this._dlg = { kind, o, fields, resolve, cancelled, busy: false };

            $('appDialogTitle').textContent = o.title || '';
            const msg = $('appDialogMsg');
            msg.textContent = o.message || '';
            msg.hidden = !o.message;
            const det = $('appDialogDetails');
            det.replaceChildren(...(o.details || []).map(d => {
                const li = document.createElement('li');
                li.textContent = d;
                return li;
            }));
            det.hidden = !(o.details && o.details.length);
            const box = $('appDialogFields');
            box.replaceChildren(...fields.map((f, i) => this._dialogField(f, i)));
            box.hidden = !fields.length;
            this._dialogError('');

            const ok = $('appDialogOk');
            ok.className = `btn btn-sm ${o.danger ? 'btn-danger' : 'btn-primary'}`;
            ok.textContent = o.confirmLabel || this.t('dialog.confirm', 'Confirm');
            ok.disabled = !!o.typed;
            const cancel = $('appDialogCancel');
            cancel.textContent = o.cancelLabel || this.t('common.cancel', 'Cancel');
            cancel.disabled = false;
            $('appDialogClose').disabled = false;

            this._dialogWire(modal);
            // a submit in flight cannot be cancelled: its request is already
            // sent, and "cancelled" would hide whether it went through
            modal._keepOpen = () => !!this._dlg?.busy;
            modal._onClose = () => this._dialogDone(cancelled);
            const first = box.querySelector('input, select');
            this.openModal('appDialog', { focus: first || (o.danger ? cancel : ok) });
            document.dispatchEvent(new CustomEvent('mbg:dialog', { detail: {
                kind, title: o.title || '', message: o.message || '', details: o.details || [],
                danger: !!o.danger, typed: o.typed || null,
                fields: fields.filter(f => !f.typed).map(f => ({ name: f.name, kind: f.kind, label: f.label || '',
                    initial: f.initial ?? '', min: f.min, max: f.max, required: f.required !== false })),
            } }));
        });
    },

    _dialogField(f, i) {
        const id = `appDialogF${i}`;
        const g = document.createElement('div');
        g.className = 'form-group';
        if (f.label) {
            const l = document.createElement('label');
            l.className = 'form-label';
            l.htmlFor = id;
            l.textContent = f.label;
            g.append(l);
        }
        const row = document.createElement('div');
        row.className = 'app-dialog-input';
        // a choice is a <select> of {value, label}; everything else is typed
        const inp = document.createElement(f.kind === 'select' ? 'select' : 'input');
        inp.id = id;
        inp.className = f.kind === 'select' ? 'input form-select' : 'input';
        inp.dataset.field = f.name;
        inp.autocomplete = 'off';
        if (f.kind === 'select') {
            inp.append(...(f.options || []).map(o => {
                const opt = document.createElement('option');
                opt.value = o.value ?? '';
                opt.textContent = o.label ?? o.value ?? '';
                return opt;
            }));
        } else {
            // a number is typed as text: a type=number input hides "abc" from
            // the page (value ''), and RO users type a decimal comma
            inp.type = f.kind === 'password' ? 'password' : 'text';
            if (f.kind === 'number') inp.inputMode = 'decimal';
            if (f.placeholder) inp.placeholder = f.placeholder;
        }
        if (f.initial !== undefined && f.initial !== null) inp.value = String(f.initial);
        row.append(inp);
        if (f.unit) {
            const u = document.createElement('span');
            u.className = 'app-dialog-unit';
            u.textContent = f.unit;
            row.append(u);
        }
        g.append(row);
        const hintText = f.hint || this._dialogRangeHint(f);
        if (hintText) {
            const h = document.createElement('div');
            h.className = 'field-hint';
            h.textContent = hintText;
            g.append(h);
        }
        const err = document.createElement('div');
        err.className = 'field-error';
        err.id = `${id}-err`;
        err.hidden = true;
        inp.setAttribute('aria-describedby', err.id);
        g.append(err);
        if (f.typed) inp.addEventListener('input', () => {
            document.getElementById('appDialogOk').disabled = inp.value.trim() !== f.typed;
        });
        return g;
    },

    _dialogRangeHint(f) {
        if (f.kind !== 'number') return '';
        const u = f.unit ? ` ${f.unit}` : '';
        if (f.min != null && f.max != null) return this.t('dialog.between', 'Between {min} and {max}', { min: f.min, max: f.max + u });
        if (f.min != null) return this.t('dialog.atLeast', 'At least {min}', { min: f.min + u });
        if (f.max != null) return this.t('dialog.atMost', 'At most {max}', { max: f.max + u });
        return '';
    },

    // the dialog's own listeners, added once (no data-action: the role
    // gating must never hide Cancel or OK)
    _dialogWire(modal) {
        if (modal._wired) return;
        modal._wired = true;
        document.getElementById('appDialogOk').addEventListener('click', () => this._dialogOk());
        const cancel = () => { if (!this._dlg?.busy) this.closeModal('appDialog'); };
        document.getElementById('appDialogCancel').addEventListener('click', cancel);
        document.getElementById('appDialogClose').addEventListener('click', cancel);
        // capture: runs before openModal's Escape handler on the same modal
        modal.addEventListener('keydown', e => {
            if (!this._dlg) return;
            if (e.key === 'Escape' && this._dlg.busy) { e.preventDefault(); e.stopPropagation(); return; }
            if (e.key !== 'Enter' || e.isComposing) return;
            // Enter on a button is that button (Cancel stays Cancel on a red
            // dialog); from a field or the dialog body it is OK
            // (stopPropagation: the page's own Enter shortcut would also save
            // an editor open under the dialog)
            if (e.target.tagName === 'BUTTON') return;
            e.preventDefault();
            e.stopPropagation();
            this._dialogOk();
        }, true);
    },

    _dialogError(text) {
        const el = document.getElementById('appDialogErr');
        el.textContent = text || '';
        el.hidden = !text;
    },

    // reads and checks every field; null when one is wrong (said under it)
    _dialogRead() {
        const st = this._dlg;
        const out = {};
        let bad = false;
        for (const f of st.fields) {
            const inp = document.querySelector(`#appDialogFields [data-field="${CSS.escape(f.name)}"]`);
            const raw = inp ? inp.value : '';
            let v = null;
            let err = '';
            const s = f.kind === 'password' ? raw : raw.trim();
            if (f.typed) {
                if (s !== f.typed) err = this.t('dialog.typeToConfirm', 'Type {text} to confirm', { text: f.typed });
            } else if (s === '') {
                if (f.required !== false) err = this.t('dialog.required', 'Required');
            } else if (f.kind === 'number') {
                // plain decimals only: Number() would also take "0x10" and "1e3"
                v = /^[-+]?(\d+([.,]\d*)?|[.,]\d+)$/.test(s) ? Number(s.replace(',', '.')) : NaN;
                if (!Number.isFinite(v)) err = this.t('dialog.notANumber', 'Not a number');
                else if ((f.min != null && v < f.min) || (f.max != null && v > f.max)) err = this._dialogRangeHint(f);
                else if (f.step && !this._dialogOnStep(v, f)) err = Number(f.step) === 1
                    ? this.t('dialog.wholeNumber', 'A whole number')
                    : this.t('dialog.stepOf', 'In steps of {step}', { step: f.step });
            } else {
                v = s;
            }
            if (!err && f.validate && !f.typed) err = f.validate(v, out) || '';
            const errEl = inp && document.getElementById(`${inp.id}-err`);
            if (errEl) { errEl.textContent = err; errEl.hidden = !err; }
            if (inp) inp.setAttribute('aria-invalid', err ? 'true' : 'false');
            if (err) { if (!bad && inp) inp.focus(); bad = true; }
            if (!f.typed) out[f.name] = v;
        }
        return bad ? null : out;
    },

    _dialogOnStep(v, f) {
        const n = (v - (f.min ?? 0)) / Number(f.step);
        return Math.abs(n - Math.round(n)) < 1e-9;
    },

    async _dialogOk() {
        const st = this._dlg;
        if (!st || st.busy) return;
        const values = this._dialogRead();
        if (!values) return;
        const submit = st.o.submit;
        if (submit) {
            const ok = document.getElementById('appDialogOk');
            const label = ok.textContent;
            st.busy = true;
            this._dialogError('');
            // focus stays inside the dialog (a disabled OK would drop it to
            // <body>, out of the dialog's own key handling)
            const box = document.querySelector('#appDialog .modal-content');
            if (box) { box.tabIndex = -1; box.focus(); }
            ok.disabled = true;
            ok.textContent = this.t('dialog.busy', 'Working…');
            document.getElementById('appDialogCancel').disabled = true;
            document.getElementById('appDialogClose').disabled = true;
            let err;
            try { err = await (st.kind === 'confirm' ? submit() : submit(values)); }
            catch (e) { err = (e && e.message) || String(e); }
            st.busy = false;
            if (this._dlg !== st) return;
            ok.disabled = false;
            ok.textContent = label;
            document.getElementById('appDialogCancel').disabled = false;
            document.getElementById('appDialogClose').disabled = false;
            // any string is a refusal, an empty one too (HTTP/2 has no status text)
            if (typeof err === 'string') { this._dialogError(err || this.t('dialog.failed', 'It did not go through')); return; }
        }
        const modal = document.getElementById('appDialog');
        modal._onClose = null;
        this.closeModal('appDialog');
        this._dialogDone(st.kind === 'confirm' ? true : values);
    },

    _dialogDone(result) {
        const st = this._dlg;
        if (!st) return;
        this._dlg = null;
        st.resolve(result);
    },

    // For tests and automation: answer the open dialog as a user would —
    // true/false for a confirm, an object of field values or null for
    // askValues. Runs the same checks and submit as OK. Answers the API-key
    // dialog first when it is open (it sits on top).
    async _dialogAnswer(x) {
        if (this._keyDlg) {
            const inp = document.getElementById('appKeyInput');
            if (x === null || x === false) { this.closeModal('appKeyDialog'); return { open: false }; }
            inp.value = typeof x === 'string' ? x : (x?.value ?? '');
            this._keyDialogOk();
            return { open: false };
        }
        const st = this._dlg;
        if (!st) return { open: false };
        if (x === null || x === false) { this.closeModal('appDialog'); return { open: false }; }
        const set = (name, v) => {
            const inp = document.querySelector(`#appDialogFields [data-field="${CSS.escape(name)}"]`);
            if (inp) inp.value = v === null || v === undefined ? '' : String(v);
        };
        if (st.o.typed) set('__typed', st.o.typed);
        if (x && typeof x === 'object') for (const [k, v] of Object.entries(x)) set(k, v);
        else if (st.kind === 'values' && x !== true && st.fields[0]) set(st.fields[0].name, x);
        await this._dialogOk();
        const open = this._dlg === st;
        const errs = open ? [...document.querySelectorAll('#appDialog .field-error')].filter(e => !e.hidden).map(e => e.textContent) : [];
        return { open, errors: errs };
    },

    // ── the API key, asked from inside a request ──
    // Exempt from "one dialog at a time" and stacked above #appDialog: a
    // Clamp or a sequence Run sends its request from the dialog's submit, and
    // a 401 there must still be able to ask. Parallel requests share one ask.
    _askApiKey() {
        if (this._keyDlg) return this._keyDlg.promise;
        const modal = document.getElementById('appKeyDialog');
        if (!modal) return Promise.resolve(null);
        let resolve;
        const promise = new Promise(r => { resolve = r; });
        this._keyDlg = { promise, resolve };
        if (!modal._wired) {
            modal._wired = true;
            document.getElementById('appKeyOk').addEventListener('click', () => this._keyDialogOk());
            const cancel = () => this.closeModal('appKeyDialog');
            document.getElementById('appKeyCancel').addEventListener('click', cancel);
            document.getElementById('appKeyClose').addEventListener('click', cancel);
            modal.addEventListener('keydown', e => {
                if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); e.stopPropagation(); this._keyDialogOk(); }
            }, true);
        }
        const inp = document.getElementById('appKeyInput');
        inp.value = '';
        modal._onClose = () => this._keyDialogDone(null);
        this.openModal('appKeyDialog', { focus: inp });
        document.dispatchEvent(new CustomEvent('mbg:dialog', { detail: {
            kind: 'apikey', title: document.getElementById('appKeyTitle').textContent, message: '', details: [],
            danger: false, typed: null, fields: [{ name: 'value', kind: 'password', label: '', initial: '', required: true }],
        } }));
        return promise;
    },

    _keyDialogOk() {
        const inp = document.getElementById('appKeyInput');
        const k = inp.value.trim();
        if (!k) { inp.setAttribute('aria-invalid', 'true'); inp.focus(); return; }
        inp.removeAttribute('aria-invalid');
        const modal = document.getElementById('appKeyDialog');
        modal._onClose = null;
        this.closeModal('appKeyDialog');
        this._keyDialogDone(k);
    },

    _keyDialogDone(k) {
        const st = this._keyDlg;
        if (!st) return;
        this._keyDlg = null;
        st.resolve(k);
    },
});
