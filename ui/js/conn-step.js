/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The connection step (ADR-0031 §2/§4, design §3 Step 2, L3.6) — augments
// JanitzaMonitor.prototype. One component says how a device is reached, in
// Add (step 2 of the steps-mode shell) and in the Edit device modal's
// Connection section. The questions come in order — 2a port, 2b where the bus
// is, 2c role on the bus (Master / Listen-only), 2d protocol, 2e line, 2f
// address; 2g CAN (with its listen-only gate), 2h HTTP (host + device id),
// 2i MQTT — each answer narrowing the next, every choice from the server
// (ConnStepModel over /api/protocols, /api/buses, /api/can-interfaces, the
// template summary and the device's locks). An option that cannot be taken
// stays, aria-disabled and focusable, its reason beside it.
//
// It is the connection form's form object (app-connform.js `_cfForm`): the
// same model (ConnformModel), the same payload, Test, field errors and
// serial port picker — only drawn and read its own way. So the Edit modal's
// draft, dry run, 412 rebase and review work on it unchanged.
//
//   window.mountConnStep(host, {context: 'add'|'edit', template, device,
//                               onChange, prefill, state, onChangeModel, px,
//                               M, prefilled})
//     → { read(), errors(), payload(), wire(), state(), fieldId(path),
//         showErrors(fields), form }
//   template: a template summary row, its id, or a function giving either
//   device:   (edit) the GET /api/devices/{id} entry; (add) a prefill
//             connection ({serial_port} | {bridge, bridge_port} | …)
//   state:    what state() returned before (Back): the answers kept, a Seplos
//             role only on the same bus and template (M7)
//   addressExtra(form, st, {asks, seplos}) → html
//             drawn at the end of 2f, the address question, every time it is
//             (the bus scan panel of "Many of the same", L3.7: app-add-many.js)
//   busId()   the /api/buses id of the bus the answers point at, or null
Object.assign(JanitzaMonitor.prototype, {

    // ── data: the matrix, the buses, the CAN interfaces, the bridges ──────
    async _csLoad(force = false) {
        const c = this._csCache;
        if (c && !force) {
            if (c.loading) return c.loading;
            if (Date.now() - (c.at || 0) < 15000) return c;
        }
        const get = async url => {
            try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch (e) { return null; }
        };
        const p = (async () => {
            const [protocols, buses, can] = await Promise.all([get('/api/protocols'), get('/api/buses'), get('/api/can-interfaces')]);
            if (force || !this._bridges || !this._bridgeTypes) await this._loadBridges();
            this._csCache = { protocols: protocols || {}, buses: (buses || {}).buses || [],
                              can: can ? (can.interfaces || []) : null, canError: (can || {}).error || '', at: Date.now() };
            return this._csCache;
        })();
        this._csCache = Object.assign({}, c || {}, { loading: p });
        return p;
    },

    _csData() {
        const c = this._csCache || {};
        return { protocols: c.protocols || {}, buses: c.buses || [], can: c.can === undefined ? null : c.can,
                 canError: c.canError || '', ready: !!c.at };
    },

    // the template summary the form is for (a row, an id, or a function)
    _csTpl(form) {
        let t = form.opts.template;
        if (typeof t === 'function') t = t();
        if (!t) return null;
        if (typeof t === 'object') return t;
        const lists = [this._devDetail?.templates, this._csTemplates, this._templates, this._devTemplates];
        for (const l of lists) {
            const hit = Array.isArray(l) ? l.find(x => x && x.id === t) : null;
            if (hit) return hit;
        }
        return { id: t, name: t };
    },

    // ── mount ─────────────────────────────────────────────────────────────
    _csMount(host, px, M, opts = {}) {
        const CS = window.ConnStepModel;
        const form = this._cfForm(host, px, M, opts);
        form.cs = { axes: { port: null, reach: null, protocol: null, framing: null, udp: false, mode: null },
                    role: null, touched: { unit: false, line: false }, busId: null, announce: '' };
        form.inputs = () => this._csInputs(form);
        form.html = () => this._csHtml(form);
        form.fieldId = path => this._csFieldId(form, path);
        form.pickerRole = () => CS.pickerRole(form.cs.axes, form.cs.role);
        form.moreErrors = changed => this._csErrors(form, changed);
        form.origAxes = CS.axesOf(form.orig);
        const entry = opts.device && opts.context === 'edit' ? opts.device : null;
        form.entry = entry;
        if (opts.context === 'edit' || opts.prefilled) {
            form.cs.axes = CS.axesOf(form.M);
            form.cs.role = opts.context === 'edit' ? form.cs.axes.mode : null;
        }
        if (opts.state && opts.state.cs) this._csRestore(form, opts.state);
        // an address the way in gave (a scan row's unit) is the answer, not a default
        if (opts.touchedUnit) form.cs.touched.unit = true;
        this._csWire(host, px);
        if (opts.context !== 'edit') this._csDefaults(form);
        this._cfRender(form);
        // read afresh at every mount: a device added or a line changed since shows
        this._csLoad(true).then(() => {
            if ((this._cf || {})[px] !== form || !form.host.isConnected) return;
            form.read();
            this._csSettle(form, { first: true });
            this._cfRender(form);
        });
        return form;
    },

    // Back: the answers kept; a Seplos role only where nothing it was given
    // for changed (M7)
    _csRestore(form, st) {
        form.M = JSON.parse(JSON.stringify(st.M));
        form.M.keepalive = st.M.keepalive;
        form.cs = Object.assign(form.cs, JSON.parse(JSON.stringify(st.cs)));
        const tpl = this._csTpl(form);
        if (form.cs.axes.protocol === 'seplos_v2' && (st.tplId || null) !== ((tpl && tpl.id) || null)) {
            form.cs.role = null;
            form.M.bus_mode = '';
        }
    },

    // one set of listeners per host: a bus picked, a field touched, a locked
    // or refused option never taken
    _csWire(host, px) {
        if (host._csWired) return;
        host._csWired = true;
        host.addEventListener('click', ev => {
            const el = ev.target.closest && ev.target.closest('input[aria-disabled="true"]');
            if (el && /^(radio|checkbox)$/.test(el.type)) {
                ev.preventDefault();
                const form = (this._cf || {})[px];
                const why = el.getAttribute('data-cs-why');
                if (form && why) this._csSay(form, why);
            }
        }, true);
        host.addEventListener('change', ev => {
            const el = ev.target;
            if (el && el.hasAttribute && el.hasAttribute('data-cs-locked') && el.tagName === 'SELECT') {
                el.value = el.getAttribute('data-cs-locked');
                ev.stopImmediatePropagation();
                const form = (this._cf || {})[px];
                if (form) this._csSay(form, el.getAttribute('data-cs-why') || '');
            }
        }, true);
        // the keyboard in a question (M3): the arrows move the focus through
        // every option — a refused one too, its reason said — and choose
        // nothing; Space or Enter chooses the focused one (a refused one only
        // says why). Tab enters the group at its chosen option.
        host.addEventListener('keydown', ev => {
            const el = ev.target;
            if (!el || el.type !== 'radio' || !el.closest('.cs-seg')) return;
            const form = (this._cf || {})[px];
            const group = [...el.closest('.cs-seg').querySelectorAll('input[type="radio"]')];
            const i = group.indexOf(el);
            const k = ev.key;
            let to = null;
            if (k === 'ArrowRight' || k === 'ArrowDown') to = group[(i + 1) % group.length];
            else if (k === 'ArrowLeft' || k === 'ArrowUp') to = group[(i - 1 + group.length) % group.length];
            else if (k === 'Home') to = group[0];
            else if (k === 'End') to = group[group.length - 1];
            if (to) {
                ev.preventDefault();
                to.focus();
                if (form && to.getAttribute('aria-disabled') === 'true') this._csSay(form, to.getAttribute('data-cs-why') || '');
                return;
            }
            if (k === ' ' || k === 'Enter') {
                ev.preventDefault();
                if (el.getAttribute('aria-disabled') === 'true') {
                    if (form) this._csSay(form, el.getAttribute('data-cs-why') || '');
                    return;
                }
                if (!el.checked) {
                    el.checked = true;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
        }, true);
        host.addEventListener('input', ev => {
            const form = (this._cf || {})[px];
            const id = ev.target && ev.target.id;
            if (!form || !id) return;
            if (id === `${px}Unit`) form.cs.touched.unit = true;
            if ([`${px}Baud`, `${px}Parity`, `${px}Stop`, `${px}Bits`].includes(id)) form.cs.touched.line = true;
        });
        host.addEventListener('change', ev => {
            const form = (this._cf || {})[px];
            const id = ev.target && ev.target.id;
            if (!form || !id) return;
            if ([`${px}Baud`, `${px}Parity`, `${px}Stop`, `${px}Bits`].includes(id)) form.cs.touched.line = true;
            // the bus may have changed: its claims, line and units judged again
            if ([`${px}Serial`, `${px}Host`, `${px}Port`, `${px}CanChannel`].includes(id)) this._csBusChanged(form);
        });
    },

    _csSay(form, text) {
        const live = document.getElementById(form.px + 'CsLive');
        if (live) { live.textContent = ''; setTimeout(() => { live.textContent = text; }, 30); }
    },

    // ── the state the questions are judged in ─────────────────────────────
    _csCtx(form) {
        const CS = window.ConnStepModel;
        const d = this._csData();
        const ax = Object.assign({}, form.cs.axes);
        if (ax.port === 'ethernet') ax.udp = ax.reach === 'address' && !!form.M.udp;
        const bus = ax.port ? CS.busFor(form.M, d.buses, (this._serialPorts || {}).ports) : null;
        const origBus = form.opts.context === 'edit' ? CS.busFor(form.orig, d.buses, (this._serialPorts || {}).ports) : null;
        const tpl = this._csTpl(form);
        const brRec = form.M.bridge ? (this._bridges || []).find(b => b.id === form.M.bridge) : null;
        const bridge = brRec ? { id: brRec.id, name: brRec.name || brRec.id, api: this._bridgeType(brRec.type).serial_config === 'api' } : null;
        const can = ax.port === 'can' && Array.isArray(d.can) ? (d.can.find(i => i.name === form.M.channel) || null) : null;
        return {
            protocols: d.protocols, axes: ax, role: form.cs.role, context: form.opts.context, tpl,
            locks: (form.entry || {}).locks || [], orig: form.opts.context === 'edit' ? form.origAxes : null,
            bus, selfId: form.opts.context === 'edit' ? form.opts.deviceId : null, bridge, can,
            canName: form.M.channel, canBitrate: form.M.bitrate, canChecked: Array.isArray(d.can),
            sameBus: !origBus || !bus ? (this._csWhere(form.M) === this._csWhere(form.orig)) : origBus.id === bus.id,
        };
    },

    // a bus as a string, for "the same bus" without the bus list
    _csWhere(M) {
        if (!M) return '';
        if (M.bridge) return `b:${M.bridge}:${M.bridge_port}`;
        if (M.serial_port && ['rtu', 'rtu_tap', 'seplos_v2'].includes(M.mode) && !M.net) return `s:${M.serial_port}`;
        return `a:${String(M.host || '').toLowerCase()}:${M.port}`;
    },

    _csState(form) {
        const CS = window.ConnStepModel;
        const t = (k, d, p) => this.t(k, d, p);
        const ctx = this._csCtx(form);
        return { ctx, opts: CS.options(ctx, t), line: CS.lineInfo(ctx, t), addr: CS.addressInfo(ctx, t),
                 gate: ctx.axes.port === 'can' ? CS.canGate(ctx.can, ctx.canName, ctx.canBitrate, t) : null };
    },

    // ── answers given: defaults where the server decides, M kept in step ─
    // a new device: what has one answer only is that answer (a template with
    // one port, CAN's one interface kind); nothing else is chosen for it
    _csDefaults(form) {
        const CS = window.ConnStepModel;
        const t = (k, d, p) => this.t(k, d, p);
        const ax = form.cs.axes;
        const ctx = () => this._csCtx(form);
        if (!this._csData().ready) return;
        if (!ax.port) {
            const okPorts = CS.portOptions(ctx(), t).filter(o => o.state === 'ok');
            if (okPorts.length === 1 && this._csTpl(form)) ax.port = okPorts[0].value;
        }
        if (ax.port && !ax.reach) {
            const okR = CS.reachOptions(ctx(), t).filter(o => o.state === 'ok');
            if (okR.length === 1) ax.reach = okR[0].value;
            else if (ax.port === 'ethernet' && okR.some(o => o.value === 'address')) ax.reach = 'address';
        }
        if (ax.port && (ax.reach || ['http', 'mqtt'].includes(ax.port)) && !ax.protocol) {
            const okP = CS.protocolOptions(ctx(), t).filter(o => o.state === 'ok');
            const tpl = this._csTpl(form);
            const want = okP.find(o => tpl && tpl.speaks && (o.value === tpl.speaks || o.value.startsWith(`${tpl.speaks}:`))
                && (!o.value.endsWith(':ascii') || (tpl.transports || []).every(x => /ascii/.test(x))));
            const pick = want || (okP.length === 1 ? okP[0] : null);
            if (pick) this._csSetProtocol(form, pick.value);
        }
        // the role a port has with no question asked: the matrix says it (a
        // port with one mode); written here until each cell says it is the
        // only one (FINDINGS L3.6-R1)
        if (ax.port === 'can') ax.mode = 'listen';
        if (ax.port === 'mqtt') ax.mode = 'listen';
        if (ax.port === 'http') ax.mode = 'poll';
        // a role the matrix alone decides (Modbus TCP is asked, never heard) is
        // that role; a serial bus's is asked or pre-filled from its claims
        if (ax.port === 'ethernet' && ax.protocol && !form.cs.role) {
            const open = CS.roleOptions(ctx(), t).filter(o => o.state === 'ok');
            if (open.length === 1) form.cs.role = open[0].value;
        }
        this._csSyncM(form);
    },

    _csSetProtocol(form, key) {
        const ax = form.cs.axes;
        const [proto, framing] = String(key).split(':');
        ax.protocol = proto;
        if (proto === 'modbus') {
            ax.framing = framing || (ax.port === 'ethernet' ? 'mbap' : this._csBridgeFraming(form) || 'rtu');
        } else ax.framing = null;
    },

    // the framing of the bridge the form is on (its type's)
    _csBridgeFraming(form) {
        const b = (this._bridges || []).find(x => x.id === form.M.bridge);
        const f = b ? this._bridgeType(b.type).framing : null;
        return { rtu: 'rtu', ascii: 'ascii', modbus_tcp: 'mbap' }[f] || null;
    },

    // M follows the answers (ConnformModel's M: what is sent stays its)
    _csSyncM(form) {
        const CS = window.ConnStepModel;
        const ax = form.cs.axes;
        if (!ax.port || (!ax.reach && !['http', 'mqtt'].includes(ax.port)) || !ax.protocol) return;
        let mode = ax.mode;
        if (ax.port === 'serial' || ax.port === 'ethernet') {
            mode = form.cs.role || (ax.protocol === 'seplos_v2' ? null : 'poll');
        }
        const before = form.M;
        form.M = CS.applyAxes(form.M, Object.assign({}, ax, { mode }));
        if (ax.reach === 'bridge') this._csFitBus(form);
        ax.mode = ax.protocol === 'seplos_v2' ? (form.cs.role || null) : mode;
        // a bridge's framing is its type's
        if (ax.reach === 'bridge' && ax.protocol === 'modbus' && ax.port === 'serial') {
            ax.framing = this._csBridgeFraming(form) || ax.framing;
            if (ax.framing === 'ascii') form.M.framing = 'ascii';
        }
        return before;
    },

    // on a bridge: a bus of a bridge of the right kind (RTU bridges for a
    // serial bus, Modbus TCP gateways for Ethernet), the first one if none
    _csFitBus(form) {
        const list = this._csBridgeBuses(form);
        const cur = list.find(b => b.bridge.id === form.M.bridge && +b.port === +form.M.bridge_port);
        if (cur) return;
        const first = list[0];
        form.M.bridge = first ? first.bridge.id : '';
        form.M.bridge_port = first ? +first.port : null;
    },

    // the bridge buses this form may be on, as /api/buses lists them
    _csBridgeBuses(form) {
        const eth = form.cs.axes.port === 'ethernet';
        const buses = this._csData().buses.filter(b => b.kind === 'bridge' && b.bridge
            && (eth ? b.bridge.framing === 'mbap' : b.bridge.framing !== 'mbap'));
        // a bus the device is on that the list has not (a missing bridge) is kept
        if (form.M.bridge && !buses.some(b => b.bridge.id === form.M.bridge && +b.port === +form.M.bridge_port)
            && form.opts.context === 'edit' && this._csWhere(form.M) === this._csWhere(form.orig)) {
            buses.unshift({ id: `missing:${form.M.bridge}`, kind: 'bridge', port: form.M.bridge_port, devices: [], missing: true,
                bridge: { id: form.M.bridge, name: `${form.M.bridge} (${this.t('devices.missingBridge', 'not found')})`, port: form.M.bridge_port } });
        }
        return buses;
    },

    // after the data or a bus change: what the server decides is set — the
    // pre-filled role (with why), the inherited line, the next free address
    _csSettle(form, o = {}) {
        if (o.first && form.opts.context !== 'edit') this._csDefaults(form);
        const st = this._csState(form);
        const busId = st.ctx.bus ? st.ctx.bus.id : null;
        const moved = busId !== form.cs.busId;
        form.cs.busId = busId;
        if (form.opts.context !== 'edit' && !form.cs.role && st.opts.prefill) {
            form.cs.role = st.opts.prefill.value;
            this._csSyncM(form);
        }
        // the line: a bus with devices gives its own (locked); a new device on
        // an empty bus takes the template's, else 9600 8N1, until typed over
        const L = window.ConnStepModel.lineInfo(this._csCtx(form), (k, d, p) => this.t(k, d, p));
        const take = L.line && (L.state === 'inherited' || L.state === 'installation'
            || (form.opts.context !== 'edit' && ['template', 'default'].includes(L.state) && !form.cs.touched.line));
        if (take) Object.assign(form.M, L.line);
        // the address: a new device takes the template's default, or the next free
        if (form.opts.context !== 'edit' && !form.cs.touched.unit && (moved || o.first)) {
            const a = window.ConnStepModel.addressInfo(this._csCtx(form));
            if (a.value !== null) form.M.unit_id = a.value;
        }
    },

    // ── answers ───────────────────────────────────────────────────────────
    // a radio of a question: port, reach, protocol, role
    _csPick(px, name, el) {
        const CS = window.ConnStepModel;
        const form = (this._cf || {})[px];
        if (!form || !el || form.rendering) return;
        form.read();                                         // what was typed is kept
        const st = this._csState(form);
        const list = { PortKind: st.opts.ports, Reach: st.opts.reaches, Speaks: st.opts.protocols, Role: st.opts.roles }[name] || [];
        const v = el.value;
        const o = list.find(x => x.value === v);
        const ax = form.cs.axes;
        const cur = { PortKind: ax.port, Reach: ax.reach, Speaks: ax.protocol === 'modbus' ? CS.protocolKey(ax) : ax.protocol, Role: form.cs.role }[name];
        if (!o || o.state !== 'ok') {
            // refused: it stays as it was, the reason said
            this._csSay(form, o ? o.reason : '');
            this._cfRender(form, cur ? `${px}${name}-${this._csSafe(cur)}` : el.id);
            return;
        }
        const seplosBefore = ax.protocol === 'seplos_v2';
        if (name === 'PortKind') {
            form.cs.axes = { port: v, reach: null, protocol: null, framing: null, udp: false, mode: null };
            form.cs.role = form.opts.context === 'edit' && v === form.origAxes.port ? form.origAxes.mode : null;
            this._csDefaults(form);
            this._csAfterBus(form, true);
        } else if (name === 'Reach') {
            ax.reach = v;
            // the protocol stays where the new reach offers it, else it is asked again
            const okP = CS.protocolOptions(this._csCtx(form), (k, d, p) => this.t(k, d, p));
            const key = ax.protocol ? CS.protocolKey(ax) : null;
            if (key && okP.some(p => p.value === key && p.state === 'ok')) this._csSetProtocol(form, key);
            else { ax.protocol = null; ax.framing = null; this._csDefaults(form); }
            this._csAfterBus(form, true);
        } else if (name === 'Speaks') {
            this._csSetProtocol(form, v);
            if (ax.protocol === 'seplos_v2' || seplosBefore) form.cs.role = null;      // Seplos: always asked (M7)
            this._csSyncM(form);
            this._csAfterBus(form, false);
        } else if (name === 'Role') {
            form.cs.role = v;
            this._csSyncM(form);
        }
        this._cfRender(form, `${px}${name}-${this._csSafe(v)}`);
        form.opts.onChange?.(form);
    },

    // the bus may have changed (a port picked or typed, an address, an
    // interface): judged once the focus has moved on, and drawn again only
    // when what the bus decides changed — the field the user went to keeps
    // the focus and its caret
    _csBusChanged(form) {
        if (form.rendering) return;
        clearTimeout(form._busT);
        form._busT = setTimeout(() => {
            if ((this._cf || {})[form.px] !== form || !form.host.isConnected || form.rendering) return;
            const key = () => JSON.stringify([form.cs.busId, form.cs.role, form.M.baudrate, form.M.parity,
                                              form.M.stopbits, form.M.bytesize, form.M.unit_id, form.M.channel]);
            const was = key();
            form.read();
            this._csAfterBus(form, false);
            if (key() !== was && !this._csPatch(form)) {
                const a = document.activeElement;
                const fid = a && form.host.contains(a) ? a.id : null;
                const caret = fid && typeof a.selectionStart === 'number' ? [a.selectionStart, a.selectionEnd] : null;
                this._cfRender(form, fid);
                const b = fid ? document.getElementById(fid) : null;
                if (b && caret) { try { b.setSelectionRange(caret[0], caret[1]); } catch (e) { /* not a text field */ } }
            }
            form.opts.onChange?.(form);
        }, 0);
    },

    // what the bus decides drawn again in place — its facts, the role, the
    // line, the address — the fields typed in (an address, a path, and the
    // marks on them) left as they are; false when the questions themselves
    // changed (then the whole step is drawn)
    _csPatch(form) {
        const { px } = form;
        const tmp = document.createElement('div');
        tmp.innerHTML = form.html();
        const parts = ['BusFacts', 'SpeaksQ', 'RoleQ', 'LineQ', 'AddrQ'];
        const has = (root, id) => !!(root === document ? document.getElementById(px + id) : root.querySelector(`#${px}${id}`));
        // a question appearing or leaving, or Test (a tap has none in Add):
        // the whole step is drawn again (L3.9-F8)
        if ([...parts, 'TestBtn'].some(id => has(document, id) !== has(tmp, id)) || form.cs.axes.port === 'can') return false;
        const a = document.activeElement;
        const fid = a && form.host.contains(a) ? a.id : null;
        let moved = false;
        for (const id of parts) {
            const cur = document.getElementById(px + id);
            if (!cur) continue;
            if (fid && cur.contains(a)) moved = true;
            cur.replaceWith(tmp.querySelector(`#${px}${id}`));
        }
        form.shown = this._cfIn(form);
        if (moved && fid) document.getElementById(fid)?.focus();
        return true;
    },

    _csAfterBus(form, axesChanged) {
        const CS = window.ConnStepModel;
        this._csSyncM(form);
        const st = this._csState(form);
        const busId = st.ctx.bus ? st.ctx.bus.id : null;
        if (busId !== form.cs.busId || axesChanged) {
            const ax = form.cs.axes;
            // a Seplos role is asked again on every bus (M7); a Modbus role
            // on a new device becomes what the bus's claims leave, or is asked
            if (ax.protocol === 'seplos_v2') {
                if (form.opts.context !== 'edit' || this._csWhere(form.M) !== this._csWhere(form.orig)) form.cs.role = null;
            } else if (form.opts.context !== 'edit') {
                form.cs.role = CS.roleAfterBusChange(ax, form.cs.role, st.opts);
            }
            this._csSyncM(form);
        }
        this._csSettle(form);
    },

    // a bus of a bridge picked (its select)
    _csBusPick(px, el) {
        const form = (this._cf || {})[px];
        if (!form || !el || form.rendering) return;
        form.read();
        const [b, p] = String(el.value || '').split('::');
        form.M.bridge = b || '';
        form.M.bridge_port = b && p !== '' ? +p : null;
        this._csSyncM(form);
        this._csAfterBus(form, false);
        this._cfRender(form, el.id);
        form.opts.onChange?.(form);
    },

    // a CAN interface picked from the host's list
    _csCanPick(px, name) {
        const form = (this._cf || {})[px];
        if (!form) return;
        form.read();
        form.M.channel = name;
        const iface = (this._csData().can || []).find(i => i.name === name);
        if (iface && iface.bitrate) form.M.bitrate = iface.bitrate;
        this._csAfterBus(form, false);
        this._cfRender(form, `${px}Can-${this._csSafe(name)}`);
        form.opts.onChange?.(form);
    },

    async _csRefresh(px) {
        const form = (this._cf || {})[px];
        if (!form) return;
        form.read();
        await this._csLoad(true);
        this._serialPorts = Object.assign({}, this._serialPorts || {}, { at: 0 });
        if ((this._cf || {})[px] !== form) return;
        this._csSettle(form);
        this._cfRender(form, `${px}CsRefresh`);
        this._csSay(form, this.t('cs.refreshed', 'The buses, bridges and interfaces were read again.'));
    },

    _csSafe(v) { return String(v ?? '').replace(/[^A-Za-z0-9_-]/g, '-'); },

    // ── reading it back ───────────────────────────────────────────────────
    _csInputs(form) {
        const { px, M } = form;
        const CS = window.ConnStepModel;
        const g = id => document.getElementById(px + id);
        const out = { mode: M.mode };
        const put = (k, id, check = false) => { const el = g(id); if (el) out[k] = check ? !!el.checked : el.value; };
        const line = () => { put('baudrate', 'Baud'); put('parity', 'Parity'); put('stopbits', 'Stop'); put('bytesize', 'Bits'); };
        switch (M.mode) {
        case 'tcp':
            if (M.bridge) put('via', 'Bus'); else { put('host', 'Host'); put('port', 'Port'); put('udp', 'Udp', true); }
            put('unit_id', 'Unit'); put('timeout', 'Timeout');
            break;
        case 'rtu-tcp':
            if (M.bridge) put('via', 'Bus'); else { put('host', 'Host'); put('port', 'Port'); }
            put('unit_id', 'Unit'); put('timeout', 'Timeout');
            break;
        case 'rtu':
            put('serial_port', 'Serial'); line(); put('unit_id', 'Unit'); put('timeout', 'Timeout');
            break;
        case 'rtu_tap':
            if (M.bridge) put('via', 'Bus'); else { put('serial_port', 'Serial'); line(); }
            put('unit_id', 'Unit');
            break;
        case 'seplos_v2':
            if (M.net) { put('host', 'Host'); put('port', 'Port'); } else { put('serial_port', 'Serial'); line(); }
            put('unit_id', 'Unit');
            if (M.bus_mode === 'poll') put('timeout', 'Timeout');
            break;
        case 'http': {
            const path = (this._csTpl(form) || {}).http_path;
            if (g('HttpHost')) out.url = CS.httpUrl(g('HttpHost').value, g('HttpId') ? g('HttpId').value : '', path);
            else put('url', 'Url');
            break;
        }
        case 'can':
            put('channel', 'CanChannel'); put('bitrate', 'CanBitrate'); put('stale_after_s', 'CanStale');
            break;
        case 'mqtt':
            put('broker', 'Broker'); put('mqtt_port', 'MqttPort'); put('topic', 'MqttTopic'); put('vars', 'MqttVars');
            put('username', 'MqttUser'); put('password', 'MqttPass');
            put('tls', 'MqttTls', true); put('tls_insecure', 'MqttTlsInsecure', true);
            put('ka_mode', 'KaMode'); put('ka_topic', 'KaTopic'); put('ka_first', 'KaFirst');
            put('ka_payload', 'KaPayload'); put('ka_every', 'KaEvery');
            break;
        // no protocol yet (an address, a port typed before the protocol is
        // answered): what is on screen is kept, never dropped (L3.9-F9)
        default:
            put('host', 'Host'); put('port', 'Port'); put('serial_port', 'Serial');
            put('unit_id', 'Unit'); put('timeout', 'Timeout');
            break;
        }
        return out;
    },

    // a server path → the field (or the question) that holds it
    _csFieldId(form, path) {
        const CM = window.ConnformModel;
        const { px, M } = form;
        const ax = form.cs.axes;
        const el = id => (id && document.getElementById(px + id) ? px + id : null);
        const first = name => { const r = document.querySelector(`#${px}CsHost input[name="${px}${name}"]`); return r ? r.id : null; };
        if (path === 'connection.port_kind') return first('PortKind');
        if (path === 'connection.reach') return first('Reach');
        if (path === 'connection.role') return first('Role');
        if (path === 'connection.protocol' && ax.port === 'serial' && !ax.protocol) return first('Speaks');
        if (path === 'connection.channel') return el('CanChannel');
        if (path === 'id' || path === 'name' || path === 'template') return null;
        const key = CM.fieldOf(path, M);
        if (!key) return null;
        const http = M.mode === 'http' && document.getElementById(px + 'HttpHost');
        const id = {
            via: 'Bus', bridge: 'Bus', bridge_port: 'Bus', host: 'Host', port: 'Port', mqtt_port: 'MqttPort',
            unit_id: 'Unit', timeout: 'Timeout', serial_port: 'Serial', baudrate: 'Baud', parity: 'Parity',
            stopbits: 'Stop', bytesize: 'Bits', url: http ? 'HttpHost' : 'Url', broker: 'Broker', topic: 'MqttTopic',
            username: 'MqttUser', password: 'MqttPass', tls: 'MqttTls', tls_insecure: 'MqttTlsInsecure', vars: 'MqttVars',  // leak-check: ok — field ids
            ka_mode: 'KaMode', ka_topic: 'KaTopic', ka_every: 'KaEvery',
            carrier: 'CanChannel', channel: 'CanChannel', bitrate: 'CanBitrate', stale_after_s: 'CanStale',
        }[key];
        if (key === 'mode') return first('PortKind');
        if (key === 'bus_mode') return first('Role');
        if (key === 'framing') return first('Speaks');
        return el(id);
    },

    // what the step itself refuses (beside ConnformModel's): a question not
    // answered, an answer the bus or the template refuses, the CAN gate. On a
    // saved device only what is being changed blocks a save.
    _csErrors(form, changed) {
        const CS = window.ConnStepModel;
        const t = (k, d, p) => this.t(k, d, p);
        const ax = form.cs.axes;
        const e = {};
        if (!ax.port) e['connection.port_kind'] = t('cs.err.port', 'Choose the port it is plugged into.');
        else if (!['http', 'mqtt'].includes(ax.port) && !ax.reach) e['connection.reach'] = t('cs.err.reach', 'Say where the bus is.');
        else if (ax.port === 'serial' && !ax.protocol) e['connection.protocol'] = t('cs.err.protocol', 'Choose the protocol the bus speaks.');
        else {
            Object.assign(e, CS.errors(this._csCtx(form), t));
            // a Seplos V2 role not answered (a new device, a bus changed): asked (M7)
            if (ax.protocol === 'seplos_v2' && !form.cs.role) {
                e['connection.mode'] = t('cf.svModeNeeded', 'Choose how the gateway reads the bus: ask the packs, or only listen.');
            }
            if (ax.port === 'serial' && ax.reach === 'bridge' && !form.M.bridge) e['connection.bridge'] = t('cs.err.bridge', 'Choose the bus: add a bridge first if there is none.');
            if (ax.port === 'ethernet' && ax.reach === 'bridge' && !form.M.bridge) e['connection.bridge'] = t('cs.err.gateway', 'Choose the gateway’s bus.');
            const path = (this._csTpl(form) || {}).http_path;
            if (form.M.mode === 'http' && path && document.getElementById(form.px + 'HttpHost')) {
                if (!String(document.getElementById(form.px + 'HttpHost').value || '').trim()) e['connection.url'] = t('cs.err.httpHost', 'Say the device’s host (its IP or name).');
                else if (/\$\{(unit_id|device_id)\}/.test(path) && !String(document.getElementById(form.px + 'HttpId')?.value || '').trim()) {
                    e['connection.url'] = t('cs.err.httpId', 'Say the device id behind the host.');
                }
            }
        }
        if (!changed) return e;
        // on a saved device: only a question its edit touches
        const keys = { 'connection.role': ['protocol', 'mode', 'serial_port', 'bridge', 'bridge_port', 'host', 'port'],
                       'connection.mode': ['mode', 'protocol', 'serial_port', 'host', 'port'],
                       'connection.channel': ['channel', 'protocol'], 'connection.url': ['url'],
                       'connection.bridge': ['bridge', 'bridge_port', 'protocol'] };
        return Object.fromEntries(Object.entries(e).filter(([p]) => !keys[p] || keys[p].some(k => changed.has(k))));
    },

    // ── drawing it ────────────────────────────────────────────────────────
    _csHtml(form) {
        const t = (k, d, p) => this.t(k, d, p);
        const { px } = form;
        const st = this._csState(form);
        const ax = st.ctx.axes;
        const d = this._csData();
        const routeNote = form.opts.context === 'edit' ? this._cfRouteNote(form.M, form.orig) : '';
        const busPart = this._csBusHtml(form, st);
        const showLine = ax.port === 'serial' && ax.reach && ax.protocol;
        const showAddr = ax.port && ['serial', 'ethernet'].includes(ax.port) && ax.reach && ax.protocol
            && (ax.port !== 'serial' || ax.reach !== 'bridge' || form.M.bridge);
        return `<div class="cs" id="${px}CsHost" data-cs-context="${this._esc(form.opts.context || 'add')}">
            <div class="sr-only" id="${px}CsLive" role="status" aria-live="polite"></div>
            ${d.ready ? '' : `<p class="field-hint cs-loading">${this._esc(t('cs.loading', 'Reading the buses, bridges and interfaces of this gateway…'))}</p>`}
            <div class="field-hint cf-route" id="${px}RouteHint" role="status" ${routeNote ? '' : 'hidden'}><i aria-hidden="true" class="bi bi-arrow-repeat"></i> <span>${this._esc(routeNote)}</span></div>
            ${this._csPortHtml(form, st)}
            ${ax.port && !['http', 'mqtt'].includes(ax.port) ? this._csReachHtml(form, st) : ''}
            ${busPart}
            ${ax.port === 'serial' && ax.reach ? this._csSpeaksHtml(form, st) : ''}
            ${ax.port && ax.reach && ax.protocol && st.opts.roles.length && ax.port !== 'can' ? this._csRoleHtml(form, st) : ''}
            ${showLine ? this._csLineHtml(form, st) : ''}
            ${showAddr ? this._csAddrHtml(form, st) : ''}
            ${ax.port === 'can' ? this._csCanHtml(form, st) : ''}
            ${ax.port === 'http' && ax.protocol ? this._csHttpHtml(form, st) : ''}
            ${ax.port === 'mqtt' && ax.protocol ? this._cfMqttHtml(form, '') : ''}
            ${ax.protocol && ['serial', 'ethernet', 'http'].includes(ax.port) ? this._csAdvancedHtml(form, st) : ''}
            ${ax.protocol && ax.port !== 'mqtt' && (form.M.mode !== 'rtu_tap' || form.opts.context === 'edit') ? this._cfTestHtml(form) : ''}
        </div>`;
    },

    // a question: a radiogroup whose every option is shown; one that cannot
    // be taken is aria-disabled (focusable), its reason listed under the group
    _csRadios(form, name, legend, list, cur, label, extra = '') {
        const { px } = form;
        const why = list.filter(o => o.state !== 'ok');
        const rows = list.map(o => {
            const id = `${px}${name}-${this._csSafe(o.value)}`;
            const on = o.value === cur;
            const offA = o.state !== 'ok';
            return `<label class="seg-btn ${on ? 'on' : ''} ${offA ? 'cs-off' : ''}"><input type="radio" id="${id}" name="${px}${name}" value="${this._esc(o.value)}"
                ${on ? 'checked' : ''} ${offA ? `aria-disabled="true" aria-describedby="${id}-why" data-cs-why="${this._esc(`${label(o.value)}: ${o.reason}`)}"` : ''}
                ${this._act('_csPick', [px, name], { el: true, on: 'change' })}><span class="s"></span> ${this._esc(label(o.value))}</label>`;
        }).join('');
        return `<fieldset class="cf-fieldset cs-q" id="${px}${name}Q"><legend class="wiz-eyebrow">${this._esc(legend)}</legend>
            <div class="seg cs-seg" role="radiogroup" aria-label="${this._esc(legend)}">${rows}</div>
            ${why.length ? `<ul class="cs-why">${why.map(o => `<li class="field-hint" id="${px}${name}-${this._csSafe(o.value)}-why"><b>${this._esc(label(o.value))}</b> — ${this._esc(o.reason)}</li>`).join('')}</ul>` : ''}
            ${extra}</fieldset>`;
    },

    _csPortLabel(v) {
        const t = (k, d) => this.t(k, d);
        return ({ serial: t('cs.port.serial', 'RS-485 / serial'), ethernet: t('cs.port.ethernet', 'Ethernet'),
                  can: t('cs.port.can', 'CAN'), http: t('cs.port.http', 'Web API (HTTP)'), mqtt: t('cs.port.mqtt', 'MQTT') })[v] || v;
    },

    // 2a the port: when the template leaves one, it is a fact (Change model)
    _csPortHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const ax = st.ctx.axes;
        const okList = st.opts.ports.filter(o => o.state === 'ok');
        const tpl = st.ctx.tpl;
        if (form.opts.context !== 'edit' && tpl && okList.length === 1 && ax.port === okList[0].value) {
            return `<fieldset class="cf-fieldset cs-q" id="${form.px}PortKindQ"><legend class="wiz-eyebrow">${this._esc(t('cs.q.port', 'Port'))}</legend>
                <p class="cs-fact"><input type="radio" class="sr-only" id="${form.px}PortKind-${ax.port}" name="${form.px}PortKind" value="${ax.port}" checked tabindex="-1" aria-hidden="true" aria-label="${this._esc(this._csPortLabel(ax.port))}">
                <b>${this._esc(this._csPortLabel(ax.port))}</b> — ${this._esc(t('cs.port.only', 'the only port {model} has.', { model: tpl.name || tpl.id }))}
                ${form.opts.onChangeModel ? `<button type="button" class="btn btn-ghost btn-sm" id="${form.px}ChangeModel" ${this._act('_csChangeModel', [form.px])}>${this._esc(t('cs.changeModel', 'Change model'))}</button>` : ''}</p>
                </fieldset>`;
        }
        const hint = ax.port === 'ethernet' ? `<p class="field-hint">${this._esc(t('cs.port.ethernetHint', 'Behind a converter or a bridge? That is RS-485: the bus is serial, the box only carries it.'))}</p>` : '';
        return this._csRadios(form, 'PortKind', t('cs.q.port', 'Port'), st.opts.ports, ax.port, v => this._csPortLabel(v), hint);
    },

    _csChangeModel(px) {
        const form = (this._cf || {})[px];
        if (form && form.opts.onChangeModel) form.opts.onChangeModel(form);
    },

    // 2b where the bus is
    _csReachHtml(form, st) {
        const t = (k, d) => this.t(k, d);
        const ax = st.ctx.axes;
        const eth = ax.port === 'ethernet';
        const label = v => ({
            here: t('cs.reach.here', 'A port on this host'),
            bridge: eth ? t('cs.reach.gateway', 'Through a Modbus TCP gateway') : t('cs.reach.bridge', 'Through a bridge or converter'),
            address: eth ? t('cs.reach.direct', 'Directly — its own IP and port') : t('cs.reach.address', 'Another address (a converter by its IP and port)'),
            socketcan: t('cs.reach.socketcan', 'A CAN interface of this host'),
        })[v] || v;
        const list = st.opts.reaches;
        if (list.length === 1 && list[0].state === 'ok' && ax.reach === list[0].value) {
            return `<p class="cs-fact" id="${form.px}ReachQ"><input type="radio" class="sr-only" id="${form.px}Reach-${ax.reach}" name="${form.px}Reach" value="${ax.reach}" checked tabindex="-1" aria-hidden="true" aria-label="${this._esc(label(ax.reach))}">
                <b>${this._esc(t('cs.q.reach', 'Where the bus is'))}:</b> ${this._esc(label(ax.reach))}</p>`;
        }
        return this._csRadios(form, 'Reach', ax.port === 'ethernet' ? t('cs.q.reachEth', 'How it is reached') : t('cs.q.reach', 'Where the bus is'),
                              list, ax.reach, label);
    },

    // the bus itself: a port here, a bridge's bus, an address
    _csBusHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const { px, M } = form;
        const ax = st.ctx.axes;
        if (!ax.port || !ax.reach || ax.port === 'can') return '';
        const facts = this._csBusFacts(form, st);
        if (ax.reach === 'here') {
            return `<div class="cs-bus" id="${px}BusQ">${this._cfSerialHtml(form)}${facts}</div>`;
        }
        if (ax.reach === 'bridge') {
            if (ax.protocol === 'seplos_v2') return '';
            const list = this._csBridgeBuses(form);
            const cur = M.bridge ? `${M.bridge}::${M.bridge_port ?? ''}` : '';
            const links = `<span class="cs-links">
                <button type="button" class="btn btn-ghost btn-sm" data-admin ${this._act('openBridgeModal', [])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${this._esc(t('bridges.add', 'Add bridge'))}</button>
                <button type="button" class="btn btn-ghost btn-sm" data-admin ${this._act('openBridgesDiscover', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${this._esc(t('bridges.find', 'Find on the LAN'))}</button>
                <button type="button" class="btn btn-ghost btn-sm" id="${px}CsRefresh" ${this._act('_csRefresh', [px])}><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${this._esc(t('cf.portsRefresh', 'Refresh'))}</button></span>`;
            if (!list.length) {
                return `<div class="cs-bus" id="${px}BusQ"><p class="field-hint cf-note">${this._esc(ax.port === 'ethernet'
                    ? t('cs.bus.noGateway', 'No Modbus TCP gateway is known yet. A gateway is a converter in “Modbus TCP to RTU” mode: add it as a bridge.')
                    : t('cs.bus.noBridge', 'No bridge yet. A bridge is the box the bus is reached through — our serial bridge on a Raspberry Pi or server, or an RS-485-to-Ethernet converter (Waveshare, USR, Elfin…).'))}</p>${links}</div>`;
            }
            const opt = b => {
                const v = `${b.bridge.id}::${b.port}`;
                const n = (b.devices || []).filter(x => x.id !== form.opts.deviceId).length;
                const load = b.load && b.load.pct != null ? ` · ${b.load.pct} %` : '';
                return `<option value="${this._esc(v)}" ${v === cur ? 'selected' : ''}>${this._esc(`${b.bridge.name || b.bridge.id} · ${b.host || ''}:${b.port}`)}${n ? ' · ' + this._esc(t('bridges.nDevices', '{n} device(s)', { n })) : ''}${this._esc(load)}</option>`;
            };
            return `<div class="cs-bus" id="${px}BusQ"><div class="form-row"><div class="form-group flex-2">
                <label class="form-label" for="${px}Bus">${this._esc(t('devices.wizard.bus', 'Bus'))}</label>
                <select id="${px}Bus" class="input" ${this._act('_csBusPick', [px], { el: true, on: 'change' })}>
                    ${cur ? '' : `<option value="" selected>${this._esc(t('devices.wizard.pickPort', '— pick a port —'))}</option>`}
                    ${list.map(opt).join('')}</select>
                ${form.opts.context === 'edit' ? `<div class="field-hint">${this._esc(t('devices.busMoveHint', 'Pick another bus to move it there; its topics and history stay.'))}</div>` : ''}
                </div></div>${links}${facts}</div>`;
        }
        // an address: a converter's (serial), the device's own (Ethernet)
        const eth = ax.port === 'ethernet';
        return `<div class="cs-bus" id="${px}BusQ"><div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="${px}Host">${this._esc(eth ? t('lbl.hostIp', 'Host / IP') : t('cf.bridgeHost', 'Bridge address'))}</label>
                <input type="text" id="${px}Host" class="input" value="${this._esc(M.host || '')}" placeholder="${eth ? '192.168.1.60' : '192.168.1.80'}" spellcheck="false" autocomplete="off"></div>
            ${this._cfNum(px, 'Port', t('lbl.port', 'Port'), M.port ?? (eth ? 502 : ''), 1, 65535, eth ? `1–65535 · ${t('common.default', 'default')} 502` : '')}
            </div>
            ${eth && ax.reach === 'address' ? `<label class="checkbox-label cf-check" title="${this._esc(t('devices.wizard.udpHint', 'Some devices and gateways answer Modbus TCP frames in UDP datagrams (usually port 502) instead of a TCP connection. Use it only when the manual says UDP.'))}">
                <input type="checkbox" id="${px}Udp" ${M.udp ? 'checked' : ''}> <span>${this._esc(t('devices.wizard.udp', 'Over UDP (datagrams instead of a connection)'))}</span></label>` : ''}
            ${facts}</div>`;
    },

    // what is known of the bus picked: its devices and roles, its load, the
    // client a bridge reports (another master on it?)
    _csBusFacts(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const b = st.ctx.bus;
        // always drawn (empty for a bus not known yet): drawn again in place
        if (!b) return `<div class="cs-busfacts-box" id="${form.px}BusFacts"></div>`;
        const self = form.opts.deviceId;
        const devs = (b.devices || []).filter(x => x.id !== self);
        const roleWord = r => (r === 'listen-only' ? t('cs.role.listenShort', 'Listen-only') : t('cs.role.masterShort', 'Master'));
        const items = devs.map(x => `${x.name || x.id}${x.unit != null ? ` (${t('cs.bus.unit', 'unit {n}', { n: x.unit })}, ${roleWord(x.role)})` : ''}`);
        const load = b.load && b.load.pct != null ? t('cs.bus.load', 'about {pct} % busy', { pct: b.load.pct }) : '';
        const client = b.bridge && b.bridge.client ? `<p class="field-hint cs-warn"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(t('cs.bus.client',
            'The bridge reports a client at {addr} on this bus: if that is not this gateway, another master may be connected.', { addr: b.bridge.client }))}</p>` : '';
        return `<div class="cs-busfacts-box" id="${form.px}BusFacts"><p class="field-hint cs-busfacts">${items.length
            ? this._esc(t('cs.bus.has', 'On this bus: {list}', { list: items.join(', ') }))
            : this._esc(t('cs.bus.empty', 'No device of this gateway is on this bus yet.'))}${load ? ' · ' + this._esc(load) : ''}</p>${client}</div>`;
    },

    // 2d the protocol on a serial bus (from the matrix)
    _csSpeaksHtml(form, st) {
        const t = (k, d) => this.t(k, d);
        const CS = window.ConnStepModel;
        const ax = st.ctx.axes;
        const label = v => ({ 'modbus:rtu': 'Modbus RTU', 'modbus:ascii': 'Modbus ASCII', modbus: 'Modbus', seplos_v2: t('cf.svSeg', 'Seplos V2 BMS') })[v] || v;
        const cur = ax.protocol === 'modbus' ? CS.protocolKey(ax) : ax.protocol;
        const list = st.opts.protocols;
        const okList = list.filter(o => o.state === 'ok');
        if (okList.length === 1 && cur === okList[0].value && st.ctx.tpl) {
            const bf = ax.reach === 'bridge' && ax.protocol === 'modbus' ? ` (${(ax.framing || 'rtu').toUpperCase()}, ${t('cs.speaks.bridgeFraming', 'as the bridge frames it')})` : '';
            return `<p class="cs-fact" id="${form.px}SpeaksQ"><input type="radio" class="sr-only" id="${form.px}Speaks-${this._csSafe(cur)}" name="${form.px}Speaks" value="${this._esc(cur)}" checked tabindex="-1" aria-hidden="true" aria-label="${this._esc(label(cur))}">
                <b>${this._esc(t('cs.q.protocol', 'Protocol'))}:</b> ${this._esc(label(cur))}${this._esc(bf)}</p>`;
        }
        return this._csRadios(form, 'Speaks', t('cs.q.protocol', 'Protocol'), list, cur, label);
    },

    // 2c the role on the bus
    _csRoleHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const label = v => (v === 'poll' ? t('cs.role.master', 'Master — the gateway polls')
            : t('cs.role.listen', 'Listen-only — another master polls; the gateway is a passive monitor'));
        const ax = st.ctx.axes;
        let note = '';
        if (form.cs.role && st.opts.prefill === null && st.opts.forced && st.opts.forced.value === form.cs.role && form.opts.context !== 'edit' && ax.protocol !== 'seplos_v2') {
            note = t('cs.role.prefilled', 'Pre-filled: {why}', { why: st.opts.forced.reason });
        } else if (!form.cs.role && ax.protocol === 'seplos_v2' && st.opts.forced) {
            note = t('cs.role.sepOnly', 'Only {role} fits here ({why}) — choose it: a Seplos role is never chosen for you.',
                     { role: label(st.opts.forced.value), why: st.opts.forced.reason });
        } else if (!form.cs.role) {
            note = ax.protocol === 'seplos_v2'
                ? t('cf.svModeHint', 'Choose first: if a master pack already polls this bus (the inter-pack bus of a bank), only listen; ask the packs only on a bus with no other master.')
                : t('cs.role.ask', 'Nobody claims this bus yet: say whether the gateway polls it or another master does (an inverter often polls its own meter).');
        }
        const hint = st.opts.hint ? `<p class="field-hint cs-hint"><i aria-hidden="true" class="bi bi-info-circle"></i> ${this._esc(st.opts.hint)}</p>` : '';
        const extra = `${note ? `<p class="field-hint cs-role-note" id="${form.px}RoleNote"><i aria-hidden="true" class="bi ${form.cs.role === 'listen' ? 'bi-ear' : 'bi-signpost'}"></i> ${this._esc(note)}</p>` : ''}${hint}`;
        return this._csRadios(form, 'Role', t('cs.q.role', 'Role on the bus'), st.opts.roles, form.cs.role, label, extra);
    },

    // 2e the line
    _csLineHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const { px, M } = form;
        const L = st.line;
        const ax = st.ctx.axes;
        const fixBtn = L.fix === 'bus' && st.ctx.bus && !String(st.ctx.bus.id).startsWith('missing:')
            ? `<button type="button" class="btn btn-ghost btn-sm" data-admin id="${px}EditBusLine" ${this._act('busLineOpen', [st.ctx.bus.id, form.opts.deviceId || ''])}><i aria-hidden="true" class="bi bi-sliders"></i> ${this._esc(t('cs.line.edit', 'Edit bus line…'))}</button>` : '';
        const legend = `<legend class="wiz-eyebrow">${this._esc(t('cs.q.line', 'Line'))}</legend>`;
        if (L.readonly) {
            return `<fieldset class="cf-fieldset cs-q" id="${px}LineQ">${legend}
                <p class="field-hint cs-lock" id="${px}LineWhy"><i aria-hidden="true" class="bi bi-lock"></i> ${this._esc(L.reason)} ${fixBtn}</p></fieldset>`;
        }
        if (ax.reach !== 'here') return '';
        const src = { inherited: t('cs.src.bus', 'set by the bus'), template: t('cs.src.template', 'the template’s'),
                      default: t('cs.src.default', 'a common default'), device: t('cs.src.device', 'its own'),
                      conflict: t('cs.src.conflict', 'the bus disagrees'),
                      installation: t('cs.src.installation', 'set where it is owned') }[L.state] || '';
        const html = this._serialLineHtml(px, M);
        // locked: aria-disabled, focusable, its value kept, the reason beside
        const locked = L.locked ? html.replace(/<select id="([^"]+)" class="input" >/g,
            (m, id) => `<select id="${id}" class="input" aria-disabled="true" aria-describedby="${px}LineWhy" data-cs-locked="__V_${id}__" data-cs-why="${this._esc(L.reason)}">`) : html;
        const filled = locked.replace(/__V_([A-Za-z0-9]+)__/g, (m, id) => this._esc(String({ [`${px}Baud`]: M.baudrate, [`${px}Parity`]: M.parity, [`${px}Stop`]: M.stopbits, [`${px}Bits`]: M.bytesize }[id] ?? '')));
        return `<fieldset class="cf-fieldset cs-q" id="${px}LineQ">${legend}
            ${filled}
            <p class="field-hint ${L.locked ? 'cs-lock' : ''}" id="${px}LineWhy">${L.locked ? '<i aria-hidden="true" class="bi bi-lock"></i> ' : ''}<span class="cs-src">${this._esc(src)}</span> ${this._esc(L.reason)} ${L.locked ? fixBtn : ''}</p></fieldset>`;
    },

    // 2f the address (unit / pack) and, when the gateway asks, its timeout
    _csAddrHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const { px, M } = form;
        const ax = st.ctx.axes;
        const seplos = ax.protocol === 'seplos_v2';
        const asks = form.cs.role !== 'listen' && !(seplos && M.bus_mode !== 'poll');
        const tpl = st.ctx.tpl;
        const a = st.addr;
        const dflt = tpl && Number.isInteger(tpl.default_unit_id) ? t('cs.addr.tpl', '{model}’s default: {n}', { model: tpl.name || tpl.id, n: tpl.default_unit_id }) : '';
        const hint = [a.text, dflt, a.full ? t('cs.addr.full', 'Every address on this bus is taken.') : ''].filter(Boolean).join(' · ');
        return `<fieldset class="cf-fieldset cs-q" id="${px}AddrQ"><legend class="wiz-eyebrow">${this._esc(seplos ? t('cf.packAddr', 'Pack address') : t('cs.q.address', 'Address'))}</legend>
            <div class="form-row">
                ${this._cfNum(px, 'Unit', seplos ? t('cf.packAddr', 'Pack address') : t('lbl.unitId', 'Unit ID'), M.unit_id, 0, 255,
                    this._esc(seplos ? t('cf.packAddrHint', 'The pack’s DIP switch address (ADR)') : `0–255`))}
                ${asks ? this._cfTimeout(px, 'Timeout', M) : ''}
            </div>
            ${hint ? `<p class="field-hint cs-addr" id="${px}AddrHint">${this._esc(hint)}</p>` : ''}
            ${typeof form.opts.addressExtra === 'function' ? form.opts.addressExtra(form, st, { asks, seplos }) : ''}</fieldset>`;
    },

    // 2g CAN: the host's interfaces, and listen-only as a hard gate
    _csCanHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const { px, M } = form;
        const d = this._csData();
        const rates = [125000, 250000, 500000, 1000000];
        if (M.bitrate != null && !rates.includes(+M.bitrate)) rates.push(+M.bitrate);
        const list = Array.isArray(d.can) ? d.can : [];
        const items = list.map(i => {
            const on = i.name === M.channel;
            const g = window.ConnStepModel.canGate(i, i.name, i.bitrate, t);
            const state = i.kind === 'vcan' ? t('cs.can.vcan', 'virtual') : [i.up ? t('cs.can.up', 'up') : t('cs.can.isDown', 'down'),
                i.listen_only ? t('cs.can.lo', 'listen-only') : t('cs.can.notLo', 'not listen-only'), i.bitrate ? `${Math.round(i.bitrate / 1000)} kbit/s` : ''].filter(Boolean).join(' · ');
            const who = (i.devices || []).filter(x => x !== form.opts.deviceId);
            return `<div class="cf-port-row"><button type="button" class="cf-port ${on ? 'on' : ''}" id="${px}Can-${this._csSafe(i.name)}" aria-pressed="${on}" ${this._act('_csCanPick', [px, i.name])}>
                <i aria-hidden="true" class="bi ${on ? 'bi-check-circle-fill' : 'bi-circle'}"></i>
                <span class="cf-port-name">${this._esc(i.name)}</span>
                <span class="sink-pill ${g.ok ? 'ok' : 'warn'}">${this._esc(state)}</span>
                ${who.length ? `<span class="sink-pill info">${this._esc(t('cs.can.readBy', 'read by {who}', { who: who.join(', ') }))}</span>` : ''}</button></div>`;
        }).join('');
        const g = st.gate;
        const gateBox = g ? `<div class="cs-gate ${g.ok ? 'ok' : 'bad'}" id="${px}CanGate" role="status">
            <i aria-hidden="true" class="bi ${g.ok ? 'bi-shield-check' : 'bi-shield-exclamation'}"></i> ${this._esc(g.text)}
            ${g.command ? `<span class="br-fix"><code class="br-fix-cmd">${this._esc(g.command)}</code>
                <button type="button" class="btn btn-secondary btn-sm" ${this._act('copyText', [g.command], { el: true })} aria-label="${this._esc(t('bridges.copyFix', 'Copy the command'))}"><i aria-hidden="true" class="bi bi-clipboard"></i></button></span>
                <span class="field-hint">${this._esc(t('cs.can.gateWhy', 'Listen-only is a hard gate: the gateway never reads CAN through an interface that can transmit. Run it on the host, then Refresh.'))}</span>` : ''}</div>` : '';
        return `<fieldset class="cf-fieldset cs-q" id="${px}CanFields"><legend class="wiz-eyebrow">${this._esc(t('cs.q.can', 'CAN interface'))}</legend>
            <div class="cs-can-head"><span class="field-hint">${this._esc(d.can === null
                ? (d.ready ? t('cs.can.unread', 'The CAN interfaces of this host could not be read.') : t('cf.portsLoading', 'Looking for the serial ports of this host…'))
                : list.length ? t('cs.can.pick', 'The CAN interfaces of this host:') : t('cs.can.none', 'This host has no CAN interface. Type its name below once it is up.'))}${d.canError ? ' ' + this._esc(d.canError) : ''}</span>
                <button type="button" class="btn btn-secondary btn-sm" id="${px}CsRefresh" ${this._act('_csRefresh', [px])}><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${this._esc(t('cf.portsRefresh', 'Refresh'))}</button></div>
            <div class="cf-ports" role="group" aria-label="${this._esc(t('cs.q.can', 'CAN interface'))}">${items}</div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="${px}CanChannel">${this._esc(t('cf.canChannel', 'Interface'))}</label>
                    <input type="text" id="${px}CanChannel" class="input" value="${this._esc(M.channel || '')}" placeholder="can0" spellcheck="false" autocomplete="off"></div>
                <div class="form-group"><label class="form-label" for="${px}CanBitrate">${this._esc(t('cf.canBitrate', 'Bitrate — set by the host, shown only'))}</label>
                    <select id="${px}CanBitrate" class="input">${rates.map(r => `<option value="${r}" ${+M.bitrate === r ? 'selected' : ''}>${r / 1000} kbit/s</option>`).join('')}</select></div>
                ${this._cfNum(px, 'CanStale', t('cf.canStale', 'Stale after (s)'), M.stale_after_s ?? 5, 1, 3600, `1–3600 · ${t('common.default', 'default')} 5`)}
            </div>
            ${gateBox}
            <p class="field-hint"><i aria-hidden="true" class="bi bi-ear"></i> ${this._esc(t('cs.can.role', 'Role on the bus: Listen-only — the gateway decodes the frames it hears and never transmits.'))}</p></fieldset>`;
    },

    // 2h HTTP: host + device id, the call's path the template's
    _csHttpHtml(form, st) {
        const t = (k, d, p) => this.t(k, d, p);
        const CS = window.ConnStepModel;
        const { px, M } = form;
        const tpl = st.ctx.tpl;
        const path = tpl && tpl.http_path;
        const parsed = path ? (M.url ? CS.httpParse(M.url, path) : { host: '', id: '' }) : null;
        if (!path || !parsed) {
            return `<fieldset class="cf-fieldset cs-q" id="${px}HttpFields"><legend class="wiz-eyebrow">HTTP / JSON</legend>
                <div class="form-group"><label class="form-label" for="${px}Url">${this._esc(t('devices.wizard.httpUrl', 'JSON endpoint URL'))}</label>
                <input type="url" id="${px}Url" class="input" value="${this._esc(M.url || '')}" placeholder="http://192.168.1.50/rpc/Shelly.GetStatus" spellcheck="false">
                <div class="field-hint">${this._esc(path ? t('cs.http.ownUrl', 'This URL is not the template’s call: it is kept as typed.') : t('devices.wizard.httpHint', 'The device polls this URL and reads values by the template’s json_path. Values arrive already scaled.'))}</div></div></fieldset>`;
        }
        const needsId = /\$\{(unit_id|device_id)\}/.test(path);
        const seen = CS.httpUrl(parsed.host || 'HOST', parsed.id || (needsId ? 'ID' : ''), path);
        return `<fieldset class="cf-fieldset cs-q" id="${px}HttpFields"><legend class="wiz-eyebrow">${this._esc(t('cs.q.http', 'Web API'))}</legend>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="${px}HttpHost">${this._esc(t('cs.http.host', 'Host'))}</label>
                    <input type="text" id="${px}HttpHost" class="input" value="${this._esc(parsed.host)}" placeholder="192.168.1.40" spellcheck="false" autocomplete="off"></div>
                ${needsId ? `<div class="form-group"><label class="form-label" for="${px}HttpId">${this._esc(t('cs.http.id', 'Device id'))}</label>
                    <input type="text" id="${px}HttpId" class="input" value="${this._esc(parsed.id)}" placeholder="1" inputmode="numeric" spellcheck="false" autocomplete="off"></div>` : ''}
            </div>
            <p class="field-hint">${this._esc(t('cs.http.path', 'The call is {model}’s: {url}', { model: tpl.name || tpl.id, url: seen }))}</p></fieldset>`;
    },

    // Advanced: every connection value with where it comes from (read only;
    // a save checks them — ADR-0031 §5)
    _csAdvancedHtml(form, st) {
        const t = (k, d) => this.t(k, d);
        // the primary meter: its address only (its PATCH refuses the other
        // keys, FINDINGS L3.11-F3 — the operator's choice)
        if (form.opts.primary) return '';
        const ax = st.ctx.axes;
        const defs = ((this._csData().protocols || {}).defaults || {})[ax.protocol] || null;
        if (!defs) return '';
        const conn = ((form.entry || {}).connection) || {};
        const srcWord = s => ({ code: t('cs.src.code', 'built in'), gateway: t('cs.src.gateway', 'the gateway’s setting'),
                                template: t('cs.src.tplShort', 'the template'), none: t('cs.src.none', 'not set') })[s] || s;
        const shown = v => (v === null || v === undefined || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v));
        const rows = Object.entries(defs).map(([k, d]) => {
            const own = form.opts.context === 'edit' && conn[k] !== undefined && conn[k] !== null && String(conn[k]) !== String(d.value);
            const v = own ? conn[k] : d.value;
            return `<div class="setup-row"><dt><code>${this._esc(k)}</code></dt><dd>${this._esc(shown(v))}
                <span class="setup-default">(${this._esc(own ? t('cs.src.device', 'its own') : srcWord(d.source))}${d.note && !own ? ' — ' + this._esc(d.note) : ''})</span></dd></div>`;
        }).join('');
        return `<details class="cs-adv" id="${form.px}CsAdv"><summary>${this._esc(t('cs.adv', 'Advanced: every value and where it comes from'))}</summary>
            <dl class="setup-dl">${rows}</dl>
            <p class="field-hint">${this._esc(t('cs.advHint', 'Changed through the API or the configuration file; a save checks each against its range.'))}</p></details>`;
    },

    // ── Edit bus line… (ADR-0031 §4): one line for every device on a bus ──
    // From the Setup tab's connection section and from a line lock's fix
    // link. The values are asked; a dry run lists the devices that change and
    // restart, confirmed in the page's dialog; then PATCH /api/buses/{bus}/line.
    async busLineOpen(busId, deviceId = '') {
        const t = (k, d, p) => this.t(k, d, p);
        let buses = [];
        try { buses = (await (await fetch('/api/buses')).json()).buses || []; } catch (e) { buses = []; }
        const bus = buses.find(b => b.id === busId)
            || (deviceId ? buses.find(b => b.kind !== 'can' && (b.devices || []).some(d => d.id === deviceId)) : null);
        if (!bus) {
            this.showToast('error', t('busline.title', 'Edit bus line'), t('busline.noBus', 'This bus is not known to the gateway any more: reload the page.'));
            return false;
        }
        const where = bus.kind === 'bridge' && bus.bridge ? `${bus.bridge.name || bus.bridge.id} :${bus.port}` : (bus.where || bus.id);
        // an installation's unit or a component on it: the line is set there (m5)
        const owned = (bus.devices || []).find(d => d.installation || d.component_of);
        if (owned) {
            this.showToast('info', t('busline.title', 'Edit bus line'), owned.installation
                ? t('cs.line.byInstallation', 'Set by the installation {inst} ({who} is one of its units): change its connection there.', { inst: owned.installation, who: owned.name || owned.id })
                : t('cs.line.byParent', 'Set by {parent} ({who} is one of its components): change its connection there.', { parent: owned.component_of, who: owned.name || owned.id }));
            return false;
        }
        // the line it has now — the devices', our bridge port's — or unknown,
        // never a guess (m4)
        let now = null;
        try {
            const r = await fetch(`/api/buses/${encodeURIComponent(bus.id)}/line`);
            now = r.ok ? ((await r.json()).line || null) : null;
        } catch (e) { now = null; }
        const unknown = { value: '', label: t('busline.unknown', '— unknown: choose —') };
        const opts = list => [...(now ? [] : [unknown]), ...list.map(v => ({ value: String(v), label: String(v) }))];
        const cur = now ? { baud: now.baudrate, parity: now.parity, bytesize: now.bytesize, stopbits: now.stopbits }
            : { baud: '', parity: '', bytesize: '', stopbits: '' };
        const names = (bus.devices || []).map(d => d.name || d.id).join(', ');
        const v = await this.askValues({
            title: t('busline.titleOf', 'Edit bus line — {where}', { where }),
            message: `${t('busline.msg', 'One line for every device on this bus ({list}). The next step lists what changes; nothing is changed before you confirm it.', { list: names || '—' })}${now ? '' : ' ' + t('busline.unknownMsg', 'Its line now cannot be read: choose each value.')}`,
            confirmLabel: t('busline.check', 'Check…'),
            fields: [
                { name: 'baudrate', label: t('lbl.baudrate', 'Baudrate'), kind: 'select', initial: String(cur.baud ?? cur.baudrate),
                  options: opts([1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]) },
                { name: 'parity', label: t('lbl.parity', 'Parity'), kind: 'select', initial: String(cur.parity),
                  options: [...(now ? [] : [unknown]), { value: 'N', label: t('busline.parN', 'N — none') }, { value: 'E', label: t('busline.parE', 'E — even') }, { value: 'O', label: t('busline.parO', 'O — odd') }] },
                { name: 'bytesize', label: t('lbl.bytesize', 'Data bits'), kind: 'select', initial: String(cur.bytesize), options: opts([7, 8]) },
                { name: 'stopbits', label: t('lbl.stopbits', 'Stop bits'), kind: 'select', initial: String(cur.stopbits), options: opts([1, 2]) },
            ],
        });
        if (!v) return false;
        const body = { baudrate: +v.baudrate, parity: v.parity, bytesize: +v.bytesize, stopbits: +v.stopbits };
        const send = async dry => {
            const r = await fetch(`/api/buses/${encodeURIComponent(bus.id)}/line${dry ? '?dry_run=true' : ''}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            return { r, d: await r.json().catch(() => ({})) };
        };
        let got;
        try { got = await send(true); } catch (e) { got = { r: { ok: false, status: 0 }, d: { detail: String(e.message || e) } }; }
        if (!got.r.ok) {
            this.showToast('error', t('busline.title', 'Edit bus line'), this._respError(got.r, got.d));
            return false;
        }
        const plan = got.d;
        if (plan.status === 'unchanged') {
            this.showToast('info', t('busline.title', 'Edit bus line'), t('busline.same', 'Every device on {where} already reads it at {line}: nothing to change.', { where, line: plan.line.to }));
            return false;
        }
        const n = (plan.restart || []).length;
        const details = (plan.devices || []).map(d => (d.from && d.from !== d.to ? `${d.name} (${d.id}): ${d.from} → ${d.to}` : `${d.name} (${d.id})`));
        if (plan.bridge) details.unshift(t('busline.bridgeLine', 'The bridge {name}, port :{port}: set to {line} through its API', { name: plan.bridge.name, port: plan.bridge.port, line: plan.line.to }));
        const ok = await this.confirmAction({
            title: t('busline.reviewTitle', 'Change the line of {where} to {line}?', { where, line: plan.line.to }),
            message: this.tn('busline.reviewMsg', n, '{n} device(s) change and restart (a short gap in their readings):'),
            details, confirmLabel: this.tn('busline.apply', n, 'Apply and restart {n} device(s)'), danger: true,
        });
        if (!ok) return false;
        try { got = await send(false); } catch (e) { got = { r: { ok: false, status: 0 }, d: { detail: String(e.message || e) } }; }
        if (!got.r.ok) {
            this.showToast('error', t('busline.title', 'Edit bus line'), this._respError(got.r, got.d));
            return false;
        }
        this.showToast('success', t('busline.title', 'Edit bus line'), this.tn('busline.done', (got.d.restarted || []).length,
            '{where}: {line} — {n} device(s) restarted.', { where, line: plan.line.to }));
        // what the page and an open editor hold is read again
        if (this._csCache) this._csCache.at = 0;
        if (this._devEdit?.open) await this._deRebase({ busLine: true });
        else if (this._devDetail && typeof this._ddvRedraw === 'function') await this._ddvRedraw(this._devDetail.id);
        return true;
    },
});

// the component's own API, for the steps-mode shell (Add) and the Edit modal
window.mountConnStep = function (host, opts = {}) {
    const app = window.app;
    if (!app || !host) return null;
    const CM = window.ConnformModel;
    const px = opts.px || (opts.context === 'edit' ? 'ddv' : 'csAdd');
    let M = opts.M || null;
    // a model handed over whole (Duplicate, a found device) is a prefilled one
    let prefilled = !!(opts.M && opts.prefilled);
    if (!M) {
        if (opts.context === 'edit' && opts.device) M = CM.model(opts.device);
        else if (opts.state && opts.state.M) M = opts.state.M;
        else {
            const pre = opts.device || opts.prefill || null;
            M = CM.blank(pre || undefined);
            prefilled = !!(pre && (pre.protocol || pre.serial_port || pre.bridge || pre.host || pre.channel || pre.url || pre.broker));
            if (!prefilled) M.mode = '';
        }
    }
    const form = app._csMount(host, px, M, Object.assign({}, opts, {
        deviceId: opts.deviceId || (opts.context === 'edit' && opts.device ? opts.device.id : null), prefilled,
    }));
    return {
        form,
        read: () => form.read(),
        errors: () => form.read().errors,
        payload: () => form.payload(),
        wire: () => form.wire(),
        dirty: () => form.dirty(),
        fieldId: p => form.fieldId(p),
        busId: () => { const st = app._csState(form); return st.ctx.bus ? st.ctx.bus.id : null; },
        stateNow: () => app._csState(form),
        showErrors: f => form.showErrors(f),
        state: () => {
            form.read();
            const tpl = app._csTpl(form);
            const M2 = JSON.parse(JSON.stringify(form.M));
            M2.keepalive = form.M.keepalive;
            return { M: M2, cs: JSON.parse(JSON.stringify(form.cs)), tplId: (tpl && tpl.id) || null };
        },
    };
};
