/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The device page as an operations view (ADR-0031 §3, L3.3) — augments
// JanitzaMonitor.prototype.
//
// The page observes and operates; it does not configure. What a device is
// is said in one technical line (_devSummaryHtml — the header, a list row
// and a bridge card draw the same line); what may be done to it is one menu
// (_devMenuItemsHtml, built by DeviceViewModel.deviceActions for the page,
// the list rows and the bridge cards, gated by role). Editing opens from that
// menu or the E key, where DeviceViewModel.EDIT_TARGETS says: the Edit device
// modal at a section (app-device-edit.js, L3.4), or an editor of the page — a
// data tab switched into editing, or a form that is no longer a tab (a unit's
// or a component's name, the meters, the attributes; the primary's
// connection and outputs) — under a bar that says it is editing, with Done
// back to the view. The Setup tab shows the same configuration read-only, to
// every role (L3-F16).
Object.assign(JanitzaMonitor.prototype, {

    // the role the page acts for (no login: every control is the admin's)
    _effRole() {
        return window.DeviceViewModel.roleOf(!!this._authEnabled, this._role);
    },

    // ── the one line ─────────────────────────────────────────────────────
    // compact: the text alone (a list row, a bridge card); else with the write
    // lock, the outputs and "via" links (the device page's header)
    _devSummaryHtml(e, opts = {}) {
        const M = window.DeviceViewModel;
        const t = (k, d, p) => this.t(k, d, p);
        const parent = e.component_of ? (this._devices || []).find(x => x.id === e.component_of) : null;
        const v = M.summaryView(e, t, { parent });
        const text = `<span class="dev-sum-text">${this._esc(v.text)}</span>`;
        if (opts.compact) return text;
        const lock = v.lock ? `<span class="dev-sum-lock ${v.lock.locked ? 'is-locked' : 'is-open'}"><i aria-hidden="true" class="bi bi-${v.lock.locked ? 'lock-fill' : 'unlock'}"></i> ${this._esc(v.lock.said)}</span>` : '';
        // green only while it publishes (L3-UX-7): on, not connected, off in Settings, off
        const outs = v.outputs.length ? `<span class="dev-sum-outs" role="list" aria-label="${this._esc(t('devices.detail.outputs', 'Outputs'))}">${v.outputs.map(o =>
            `<span role="listitem" class="dev-sum-out ${o.on ? 'on' : 'off'} is-${this._esc(o.state || (o.on ? 'on' : 'off'))}" title="${this._esc(o.said)}"><span class="o-dot" aria-hidden="true"></span><span aria-hidden="true">${this._esc(o.label)}</span><span class="sr-only">${this._esc(o.said)}</span></span>`).join('')}</span>` : '';
        const via = v.via.map(x => {
            if (x.kind === 'bridge') {
                return `<a href="#/devices" data-via="bridge" ${this._act('ddvOpenBridge', [x.id])}><i aria-hidden="true" class="bi bi-hdd-network"></i> ${this._esc(t('ddv.via.bridge', 'bridge {id}', { id: x.id }))}${x.port != null ? ` :${this._esc(x.port)}` : ''}</a>`;
            }
            if (x.kind === 'parent') {
                return `<a href="#/devices/${encodeURIComponent(x.id)}" data-via="parent"><i aria-hidden="true" class="bi bi-cpu"></i> ${this._esc(x.name || x.id)}</a>`;
            }
            return `<a href="#/installations/${encodeURIComponent(x.id)}" data-via="installation"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(x.name || x.id)}</a>`;
        }).join(' ');
        return `<div class="dev-sum">${text}<span class="dev-sum-pills">${lock}${outs}${via
            ? `<span class="dev-sum-via">${this._esc(t('ddv.via', 'via'))} ${via}</span>` : ''}</span></div>`;
    },

    // a bridge "via" link: the Devices list, at that bridge's card
    async ddvOpenBridge(id) {
        await this.closeDeviceDetail(true);
        const card = document.querySelector(`.bridge-card[data-bridge="${CSS.escape(id)}"]`);
        if (card) {
            card.scrollIntoView({ block: 'start' });
            card.tabIndex = -1;
            card.focus({ preventScroll: true });
        }
    },

    // a hint that names an admin's menu item: the way, to an admin; who does
    // it, to anyone else (L3-UX-8, ADR-0032 §10)
    _roleHint(key, fallback, otherKey, otherFallback, params) {
        return window.DeviceViewModel.allows(this._effRole(), 'admin')
            ? this.t(key, fallback, params) : this.t(otherKey, otherFallback, params);
    },

    // ── the ⋯ menu (ADR-0032): one builder, every surface ─────────────────
    // what the builder is told about a device where it is drawn: the page
    // knows its view and what it has asked; a row knows what the list asked
    // (undefined: not answered yet — the item says "checking…")
    _devMenuCtx(e, where) {
        const M = window.DeviceViewModel;
        const page = where === 'page' && this._devDetail?.id === e.id;
        const tpls = (page ? this._devDetail?.templates : null) || this._devTplCache || [];
        const tpl = tpls.find(x => x.id === e.template) || null;
        const known = (this._ddvFacts || {})[e.id] || {};
        const usedBy = page ? (this._devDetail.usedBy ? this._ubCount(this._devDetail.usedBy)
                                                      : this._devDetail.usedByUnknown ? null : undefined)
            : known.usedBy !== undefined ? known.usedBy
            : this._ubCountsMap ? this._ubRowCount(e.id) : undefined;
        const tplUpdate = page ? this._devDetail.tplPending : known.tplUpdate;
        return {
            where,
            view: page ? this._ddvViewState() : null,
            gateway: this._gwWrites ? { writes: this._gwWrites.enabled, credential: this._gwWrites.credential } : undefined,
            usedBy, tplUpdate,
            holdsComponents: M.holdsComponents(e, tpl),     // one rule, page and list
            can: { duplicate: true, export: false },       // export not built yet: hidden, never shown disabled
            kindLabel: this._kindLabel ? (k => this._kindLabel(k || 'other')) : null,
            bridgeLineApi: this._bridges ? (id => {
                const br = (this._bridges || []).find(b => b.id === id);
                return !!br && this._bridgeType(br.type).serial_config === 'api';
            }) : undefined,
        };
    },

    // the device as a surface knows it (the page's entry, or the list's)
    _ddvEntry(id) {
        const onPage = this._view?.type === 'device' && this._devDetail?.id === id;
        const e = (onPage ? { ...(this._devDetail.entry || {}), id } : null) || (this._devices || []).find(x => x.id === id) || { id };
        return this._ddvWithParent(e);
    },

    // a component with its device's name and whether that device is on (a
    // component of a device that is off is off: its Enable says so)
    _ddvWithParent(e) {
        if (!e.component_of) return e;
        const p = (this._devices || []).find(x => x.id === e.component_of);
        return p ? { ...e, parent_name: e.parent_name || p.name || p.id, parent_enabled: p.enabled !== false } : e;
    },

    _ddvItems(e, where) {
        const t = (k, d, p) => this.t(k, d, p);
        e = this._ddvWithParent(e);
        return window.DeviceViewModel.deviceActions(e, this._effRole(), this._devMenuCtx(e, where), t);
    },

    // one item, drawn: an item that cannot run now stays focusable and says
    // why under its name; choosing it goes where that is changed, or says why
    _devMenuItemHtml(it, e, where) {
        const gate = { admin: 'data-admin', operator: 'data-live', viewer: '' };
        // the reason is said once: the item's description (shown under its
        // name, kept out of the name itself)
        const whyId = it.reason ? `mwhy-${where}-${String(e.id).replace(/[^\w-]/g, '_')}-${it.id}` : '';
        const described = whyId || it.describedBy;
        const cls = [it.danger ? 'mbg-menu-danger' : '', it.checking ? 'is-checking' : ''].filter(Boolean).join(' ');
        return `
            <button type="button" role="menuitem" data-ddv-act="${it.id}" ${gate[it.need] || ''}${it.domId ? ` id="${it.domId}"` : ''}${cls ? ` class="${cls}"` : ''}${it.disabled ? ' aria-disabled="true"' : ''}${described ? ` aria-describedby="${described}"` : ''}${it.shortcut ? ` aria-keyshortcuts="${it.shortcut}"` : ''}
                ${this._act(it.disabled ? 'ddvWhy' : 'ddvAct', [it.id, e.id, where])}><i aria-hidden="true" class="bi bi-${it.icon}"></i> <span class="mbg-menu-label">${this._esc(it.label)}${it.reason
                    ? `<small class="mbg-menu-why" aria-hidden="true" id="${whyId}">${this._esc(it.reason)}${it.goto ? ' <i aria-hidden="true" class="bi bi-arrow-right-short"></i>' : ''}</small>` : ''}</span>${it.shortcut ? `<kbd aria-hidden="true">${it.shortcut}</kbd>` : ''}</button>`;
    },

    _devMenuItemsHtml(e, where) {
        return this._ddvItems(e, where).map((g, i) => (i ? '<div role="separator" class="mbg-menu-sep"></div>' : '')
            + g.items.map(it => this._devMenuItemHtml(it, e, where)).join('')).join('');
    },

    // a menu drawn again in place (a fact arrived): the same items, the
    // focus kept on the one it was on
    _devMenuRedraw(menu, e, where) {
        if (!menu) return;
        const on = menu.contains(document.activeElement) ? document.activeElement.dataset.ddvAct : '';
        menu.innerHTML = this._devMenuItemsHtml(e, where);
        if (on && !menu.hidden) menu.querySelector(`[data-ddv-act="${CSS.escape(on)}"]`)?.focus();
    },

    // a row's ⋯ (a list row, a table row, a bridge line, an installation's
    // unit): its items are built when it opens
    _devRowMenuHtml(d, where) {
        const id = String(d.id);
        const mid = `devMenu-${where}-${id}`;
        return `<span class="mbg-menu-wrap dev-row-menu">
            <button type="button" class="btn btn-ghost btn-sm" data-dev-menu="${this._esc(id)}" aria-haspopup="menu" aria-expanded="false"
                aria-controls="${this._esc(mid)}" ${this._act('devMenuOpen', [id, where], { el: true })}
                ${this._ib(this.t('ddv.actionsShort', 'Actions'), d.name || id)}><i aria-hidden="true" class="bi bi-three-dots"></i></button>
            <div class="mbg-menu" role="menu" id="${this._esc(mid)}" hidden></div></span>`;
    },

    devMenuOpen(id, where, btn) {
        const menu = btn && document.getElementById(btn.getAttribute('aria-controls'));
        if (!menu) return;
        if (menu.hidden) {
            const e = this._ddvEntry(id);
            menu.setAttribute('aria-label', this.t('ddv.actions', 'Actions for {name}', { name: e.name || id }));
            menu.innerHTML = this._devMenuItemsHtml(e, where);
            this._ddvAskFacts(e, where, menu);
        }
        this._menuToggle(btn);
    },

    // what a row's menu could not know yet, asked once: what uses the device
    // (Delete…), whether its template changed (Update from template…). The
    // items are there already, "checking…"; they are drawn again when the
    // answers come — never added, never removed (ADR-0032 §2)
    async _ddvAskFacts(e, where, menu) {
        const ctx = this._devMenuCtx(e, where);
        const facts = (this._ddvFacts = this._ddvFacts || {});
        const own = (facts[e.id] = facts[e.id] || {});
        const asks = [];
        if (ctx.usedBy === undefined) {
            // a failed ask is "not known" (null), never "free"
            asks.push(this._ubLoad('device', e.id).then(d => { own.usedBy = this._ubCount(d); }).catch(() => { own.usedBy = null; }));
        }
        const M = window.DeviceViewModel;
        if (ctx.tplUpdate === undefined && e.template && ['device', 'meter-only'].includes(M.kindOf(e))) {
            asks.push(fetch(`/api/devices/${encodeURIComponent(e.id)}/template-changes`)
                .then(r => (r.ok ? r.json() : {})).then(p => { own.tplUpdate = !!p.pending; })
                .catch(() => { own.tplUpdate = false; }));
        }
        if (!asks.length) return;
        await Promise.all(asks);
        if (menu.isConnected) this._devMenuRedraw(menu, e, where);
    },

    // the page's ⋯: drawn again as it opens (the item E opens follows the
    // tab), and whenever a fact arrives
    ddvMenuOpen(btn) {
        const menu = document.getElementById('ddvActionsMenu');
        const s = this._devDetail;
        if (menu && s && menu.hidden) menu.innerHTML = this._devMenuItemsHtml({ ...(s.entry || {}), id: s.id }, 'page');
        this._menuToggle(btn);
    },

    _ddvRefreshMenu() {
        const s = this._devDetail;
        if (!s) return;
        const e = { ...(s.entry || {}), id: s.id };
        this._devMenuRedraw(document.getElementById('ddvActionsMenu'), e, 'page');
        const head = document.getElementById('ddvHeadActions');
        if (head) head.innerHTML = this._ddvHeadHtml(e);
    },

    // the header's operate buttons (ADR-0032 §5): the menu's own items —
    // Test and Preview always, Write… and Run a command… where the kind has
    // them; one that cannot run now is shown, disabled, saying why
    _ddvHeadHtml(e) {
        const t = (k, d, p) => this.t(k, d, p);
        e = this._ddvWithParent(e);
        const items = window.DeviceViewModel.headerActions(e, this._effRole(), this._devMenuCtx(e, 'page'), t);
        const short = { test: t('ddv.test', 'Test'), preview: t('pv.button', 'Preview'),
                        command: t('ddv.runCommand', 'Run command…'), write: t('ddv.writeValue', 'Write…') };
        const ids = { test: 'ddvHeadTest', preview: 'ddvHeadPreview', command: 'ddvHeadCommand', write: 'ddvHeadWrite' };
        const tip = { preview: t('pv.buttonTip', 'Read it once now, through its own connection, within the preview budget: values with their quality, its identity, the load') };
        return items.map(it => {
            const id = ids[it.id];
            const title = it.reason || tip[it.id] || '';
            return `<button data-live type="button" class="btn ${it.id === 'test' || it.id === 'preview' ? 'btn-secondary' : 'btn-ghost'} btn-sm" id="${id}"
                ${it.disabled ? `aria-disabled="true" aria-describedby="${id}-why"` : ''}${title ? ` title="${this._esc(title)}"` : ''}
                ${this._act(it.disabled ? 'ddvWhy' : 'ddvAct', [it.id, e.id, 'page'], { el: true })} aria-label="${this._esc(short[it.id])}"><i aria-hidden="true" class="bi bi-${it.icon}"></i> <span class="ddv-btn-label">${this._esc(short[it.id])}</span></button>${it.disabled
                ? `<span class="sr-only" id="${id}-why">${this._esc(it.reason)}</span>` : ''}`;
        }).join('');
    },

    // a disabled item chosen: where it is changed (its goto), else why, said
    async ddvWhy(item, id, where = 'page') {
        const e = this._ddvEntry(id);
        const it = this._ddvItems(e, where).flatMap(g => g.items).find(x => x.id === item);
        if (!it) return;
        if (!it.disabled) return this.ddvAct(item, id, where);
        // a Delete held while used asks the gateway again (the page may be
        // older than it): free now, its dialog opens; else what uses it is shown
        if (item === 'delete' && it.goto === 'usedby') return this.deleteDevice(id);
        if (it.goto === 'usedby') return this.usedByOpen('device', id, 'usedby');
        if (it.goto === 'security' || it.goto === 'security-login') {
            if (this.navigateTo) await this.navigateTo('config');
            this.switchConfigTab?.('security');
            // the login is switched on Accounts (an API key is the environment's)
            this.securityTab?.(it.goto === 'security-login' ? 'accounts' : 'writes');
            return;
        }
        if (it.goto) return this.ddvAct(it.goto, id, where);
        this.showToast('info', it.label, it.reason);
    },

    // one item: what it does, from the page, a list row, a bridge line or an
    // installation's unit row — the same wherever it is chosen
    async ddvAct(item, id, where = 'page', el = null) {
        const M = window.DeviceViewModel;
        const onPage = () => this._view?.type === 'device' && this._devDetail?.id === id;
        const e = this._ddvEntry(id);
        const kind = M.kindOf(e);
        const page = async () => onPage() || await this.openDeviceDetail(id);
        switch (item) {
        case 'open-installation': return this.openEndpointDetail(e.endpoint_id);
        case 'open-parent': return this.openDeviceDetail(e.component_of);
        case 'setup':
            if (!await page()) return;
            this._switchDeviceTab('setup', { section: 'connection', focusSection: true });
            return;
        case 'test': return this.testDevice(id, el || (onPage() ? document.getElementById('ddvHeadTest') : null));
        case 'preview': return this.ddvPreview(id);
        case 'write': return this.ddvWrite(id);
        case 'command':
            if (!await page()) return;
            this._switchDeviceTab('commands');
            requestAnimationFrame(() => document.querySelector('#deviceDetailView [data-dpanel="commands"] .cmd-run:not([disabled])')?.focus());
            return;
        case 'download-map': return this.ddvDownloadMap(id);
        case 'bus-line': return this.busLineOpen('', id);
        case 'duplicate': return this.duplicateDevice(id);
        case 'reconnect': return this.ddvReconnect(id);
        case 'template-update': return this.openTemplateUpdate('device', id);
        case 'unlock': return this.ddvSetWriteLock(id, false);
        case 'lock': return this.ddvSetWriteLock(id, true);
        // a component is changed through its parent
        case 'disable': return kind === 'component' ? this.switchComponentUi(id, false, e.component_of) : this.ddvSetEnabled(id, false);
        case 'enable': return kind === 'component' ? this.switchComponentUi(id, true, e.component_of) : this.ddvSetEnabled(id, true);
        case 'delete': return kind === 'component' ? this.removeComponentUi(id, e.component_of) : this.deleteDevice(id);
        case 'restore': return this.ddvOpenRestore();
        default: break;
        }
        if (item !== 'edit' && item !== 'rename' && !M.editTarget(item)) return;
        if (!await page()) return;
        return this.ddvEdit(item);
    },

    // Download map: the template's map, or (no template) the device's own
    // selection, read where its measurements are
    async ddvDownloadMap(id) {
        const e = this._ddvEntry(id);
        if (e.template || this._regDevice === id) return this.downloadRegisterMap(id);
        if (!(this._view?.type === 'device' && this._devDetail?.id === id) && !await this.openDeviceDetail(id)) return;
        await this._switchDeviceTab('measurements');
        this.downloadRegisterMap(id);
    },

    // Deleted devices: restore… — the Restore dialog, over the page or the
    // list (ADR-0032 §8; app-restore.js)
    async ddvOpenRestore() {
        return this.openRestoreDialog();
    },

    // ── editing: the Edit modal, or an editor of the page ─────────────────
    _ddvViewState() {
        const tab = document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab || 'overview';
        return { tab, section: tab === 'setup' ? (this._setupSection || '') : '' };
    },

    _ddvEditLabel(key) {
        const t = (k, d) => this.t(k, d);
        return ({
            connection: t('ddv.tabConnection', 'Connection'), rename: t('devices.unit.identity', 'Name'),
            measurements: t('devices.registers', 'Measurements'), calculated: t('calc.tab', 'Calculated'),
            commands: t('ddv.edit.commands', 'Commands & bindings'), meters: t('meters.tab', 'Meters'),
            outputs: t('devices.detail.outputs', 'Outputs'), about: t('attr.title', 'Attributes'),
            components: t('ddv.setup.components', 'Components'),
        })[key] || key;
    },

    // open the editor an item names (E: the one for what is in view) — the
    // Edit device modal at its section, or an editor of the page
    ddvEdit(key) {
        const s = this._devDetail;
        const M = window.DeviceViewModel;
        if (!s || !M.allows(this._effRole(), 'admin')) return;
        const e = { ...(s.entry || {}), id: s.id };
        // Edit device… opens the modal at the section in view (while an
        // editor is open: of the view it was opened from), else at its first
        if (key === 'edit') key = M.editSectionFor(e, this._ddvEditing ? (this._ddvEditReturn || {}) : this._ddvViewState());
        const tg = M.editTarget(key);
        if (!tg) return;
        if (tg.modal) return this._ddvEditInModal(tg);
        if (!this._ddvEditing) this._ddvEditReturn = this._ddvViewState();
        if (tg.full) return this._ddvEditFull(key, tg);
        if (tg.tab) this._switchDeviceTab(tg.tab, { editing: key });
        else this._switchDeviceTab('setup', { editing: key, editor: tg.panel, section: tg.section });
        const title = document.getElementById('ddvEditTitle');
        if (tg.focus) document.querySelector(tg.focus)?.scrollIntoView({ block: 'nearest' });
        title?.focus({ preventScroll: !!tg.focus });
    },

    // ── the large editors: full screen, the edit bar, one Save (ADR-0032 §6)
    // Measurements (its parts: Measurements · Calculated, the map's tools in
    // it) and Commands & bindings. The same editor wherever it is opened from:
    // the page, a list row's ⋯, the dashboard's shortcut (jumpToDeviceRegisters).
    // Nothing in it is written until Save; Done with edits not saved asks.
    _FULL_PART_TAB: { measurements: 'measurements', calculated: 'calculated', commands: 'commands' },

    _ddvEditFull(key, tg) {
        const tabOf = part => this._FULL_PART_TAB[part] || part;
        let part = tg.full === 'measurements' ? (tg.part === 'calculated' ? 'calculated' : 'measurements') : tg.full;
        // a device measured by its meters reads nothing itself: its Calculated part only
        if (part === 'measurements' && !document.querySelector('#deviceWsTabs [data-dtab="measurements"]')) part = 'calculated';
        this._ddvFullEditor = tg.full;
        this._switchDeviceTab(tabOf(part), { editing: key, full: true });
        this._ddvEditPartsShow(part);
        document.getElementById('ddvEditTitle')?.focus({ preventScroll: true });
    },

    // the editor's other part (Measurements ↔ Calculated): the same editor,
    // what was staged in the part left is kept
    ddvEditPart(part) {
        if (!this._ddvEditing || this._ddvFullEditor !== 'measurements') return;
        const key = part === 'calculated' ? 'calculated' : 'measurements';
        this._switchDeviceTab(this._FULL_PART_TAB[part], { editing: key, full: true });
        this._ddvEditPartsShow(part);
    },

    _ddvEditPartsShow(part) {
        const box = document.getElementById('ddvEditParts');
        if (!box) return;
        const both = this._ddvFullEditor === 'measurements' && !!document.querySelector('#deviceWsTabs [data-dtab="measurements"]');
        box.hidden = !both;
        box.querySelectorAll('[data-edit-part]').forEach(b => {
            const on = b.dataset.editPart === part;
            b.classList.toggle('active', on);
            b.classList.toggle('on', on);
            b.setAttribute('aria-pressed', String(on));
        });
    },

    // a full editor holds the screen (review of L3.11b, m1 and n3): the page
    // behind it inert — the skip link, the navigation, everything around the
    // device view — so Tab stays in it; the template notes drawn under its
    // edit bar, not over it. Off: all as it was
    _ddvFullTrap(on) {
        const view = document.getElementById('deviceDetailView');
        for (const el of this._ddvInert || []) el.inert = false;
        this._ddvInert = [];
        const notes = view && view.querySelector('.tpl-notes-fold');
        if (on && view) {
            const stop = document.querySelector('.window') || document.body;
            for (let n = view; n && n !== stop && n.parentElement; n = n.parentElement) {
                for (const sib of n.parentElement.children) {
                    // (the register editor lives beside the view and is drawn
                    // into it: never made inert)
                    if (sib !== n && !sib.inert && !sib.classList.contains('modal') && sib.id !== 'toast-container'
                        && sib.id !== 'deviceRegistersView') {
                        sib.inert = true;
                        this._ddvInert.push(sib);
                    }
                }
            }
            const skip = document.querySelector('.skip-link');
            if (skip && !skip.inert) { skip.inert = true; this._ddvInert.push(skip); }
            const bar = document.getElementById('ddvEditBar');
            if (notes && bar && !this._ddvNotesHome) {
                this._ddvNotesHome = document.createComment('tpl-notes');
                notes.before(this._ddvNotesHome);
                bar.after(notes);
            }
        } else if (this._ddvNotesHome) {
            const moved = view && view.querySelector('.tpl-notes-fold');
            if (moved && this._ddvNotesHome.isConnected) this._ddvNotesHome.replaceWith(moved);
            else this._ddvNotesHome.remove();
            this._ddvNotesHome = null;
        }
    },

    // the full editor holds staged edits not saved
    _ddvFullDirty() {
        if (!this._ddvEditing || !document.getElementById('deviceDetailView')?.classList.contains('ddv-full')) return false;
        if (this._ddvFullEditor === 'measurements') return !!this._regDirty || !!(this._calc && this._calc.dirty);
        if (this._ddvFullEditor === 'commands') return this._cmdDirty();
        return false;
    },

    // the edit bar's one Save
    async ddvEditSave() {
        const btn = document.getElementById('ddvEditSave');
        const fb = document.getElementById('ddvEditFeedback');
        if (!btn || btn.dataset.busy) return;
        const t = (k, d, p) => this.t(k, d, p);
        if (!this._ddvFullDirty()) {
            if (fb) { fb.textContent = t('devices.nothingToSave', 'Nothing to save — nothing was sent.'); fb.className = 'save-feedback ok'; }
            return;
        }
        btn.dataset.busy = '1';
        btn.setAttribute('aria-disabled', 'true');
        if (fb) { fb.textContent = t('common.saving', 'Saving...'); fb.className = 'save-feedback'; }
        let rep;
        try {
            rep = this._ddvFullEditor === 'commands' ? await this._cmdSaveStaged() : await this._regSaveStaged();
        } finally {
            delete btn.dataset.busy;
            btn.removeAttribute('aria-disabled');
        }
        const L = ids => ids.join(', ');
        const said = rep.failed
            ? (rep.saved.length
                ? t('ddv.full.partial', 'Saved: {saved}. Not saved: {failed} — {error}. What was not saved is still here: Save again.',
                    { saved: L(rep.saved), failed: L([rep.failed.id, ...rep.skipped]), error: rep.failed.error })
                : t('ddv.full.failed', 'Not saved: {failed} — {error}', { failed: rep.failed.id, error: rep.failed.error }))
            : t('ddv.full.saved', 'Saved: {saved}.', { saved: L(rep.saved) });
        if (fb) { fb.textContent = said; fb.className = `save-feedback ${rep.failed ? 'err' : 'ok'}`; }
        this.showToast(rep.failed ? (rep.saved.length ? 'warning' : 'error') : 'success', this._ddvEditLabel(this._ddvEditing), said);
    },

    // the edit bar: what is edited, how it saves (a full editor: its one
    // Save), the way back
    _ddvEditBarShow(key, full = false) {
        const bar = document.getElementById('ddvEditBar');
        if (!bar) return;
        bar.hidden = !key;
        const save = document.getElementById('ddvEditSave');
        if (save) save.hidden = !full;
        const fb = document.getElementById('ddvEditFeedback');
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        if (!full) { const p = document.getElementById('ddvEditParts'); if (p) p.hidden = true; }
        if (!key) return;
        document.getElementById('ddvEditTitle').textContent = this.t('ddv.editing', 'Editing: {what}',
            { what: this._ddvEditLabel(full && this._ddvFullEditor === 'measurements' ? 'measurements' : key) });
        const hint = document.getElementById('ddvEditHint');
        if (hint) hint.textContent = full
            ? this.t('ddv.editingFullHint', 'Nothing is written until Save. Done returns to the view (it asks about edits not saved).')
            : this.t('ddv.editingHint', 'Each part saves with its own button, as before. Done returns to the view.');
    },

    // the Edit device dialog, over an editor of the page: what that editor
    // holds unsaved is asked about first (Discard draws the page again from
    // what is stored), never left typed under the dialog
    async _ddvEditInModal(tg) {
        const s = this._devDetail;
        if (this._ddvEditing && this._ddvEditDirty()) {
            if (!await this._confirmDiscard()) return false;
            this._ddvFullDrop();
            if (!s || !await this.openDeviceDetail(s.id)) return false;
        }
        return this.devEditOpen(tg.modal, { focus: tg.focus });
    },

    // ── one leave guard (review of L3.11b, M1) ─────────────────────────────
    // Whatever leaves the device page or its editor — Back, a hash change, a
    // nav tab, another device, Back to the list — asks first about what is
    // typed and not saved: the Edit device modal's (its own guard), a full
    // editor's staged edits, an editor of the page. true: it may go on (the
    // edits dropped); false: Keep editing.
    async _ddvLeave() {
        if (this._devEdit?.open && this._deLeave) return this._deLeave();
        const dirty = this._ddvFullDirty() || (!!this._ddvEditing && this._ddvEditDirty());
        if (!dirty) return true;
        if (this._ddvLeaving) return this._ddvLeaving;           // one question for one move
        this._ddvLeaving = this._confirmDiscard();
        let ok;
        try { ok = await this._ddvLeaving; } finally { this._ddvLeaving = null; }
        if (ok) this._ddvFullDrop();
        return !!ok;
    },

    // before a page opens (another device, an installation): the Edit
    // modal's edits asked about by its own guard (a link inside it: a
    // component's device, a unit's installation — second review R2-1),
    // else the page's editors'. true: it may open
    async _ddvBeforeOpen() {
        if (this._devEdit?.open && this._deLeave) return this._deLeave();
        return this._ddvLeave();
    },

    // what the editors hold staged, dropped (the page is drawn again from
    // what is stored by whoever goes on) — the typed binding and channel
    // fields too, so the move asks once (second review R2-3)
    _ddvFullDrop() {
        this._regDirty = false;
        if (this._calc) { this._calc.dirty = false; this._calc.editing = null; }
        this._cmdStaged = {};
        if (this._ddvEdited && this._ddvEdited.clear) this._ddvEdited.clear();
        document.querySelectorAll('#deviceDetailView .cb-form [data-cb-field]').forEach(inp => { inp.value = inp.defaultValue; });
    },

    // Done: back to the view of what was edited
    // Done: what was typed and not saved is asked about (Keep editing stays;
    // Discard draws the device again from what is stored), then the view
    async ddvEditDone() {
        const M = window.DeviceViewModel;
        const s = this._devDetail;
        const dirty = this._ddvEditDirty();
        if (dirty && !await this._confirmDiscard()) return;
        if (dirty) this._ddvFullDrop();
        const tg = M.editTarget(this._ddvEditing) || {};
        // a full editor returns to the tab of the part shown (its view)
        const fullTab = tg.full ? (document.querySelector('#deviceWsTabs .config-main-tab.active')?.dataset.dtab || 'overview') : '';
        if (this._calc) this._calc.editing = null;       // an open formula editor closes with its editor
        if (dirty && s && !await this.openDeviceDetail(s.id)) return;
        if (fullTab) this._switchDeviceTab(fullTab);
        else if (tg.tab) this._switchDeviceTab(tg.tab);
        else this._switchDeviceTab('setup', { section: tg.section || '' });
        // a full editor was opened from ⋯ (or E): the focus goes back there
        if (fullTab) document.getElementById('ddvActions')?.focus({ preventScroll: true });
        else this._ddvFocusTab();
    },

    // the focus on the tab shown (a group of one tab: its group), else ⋯
    _ddvFocusTab() {
        const shown = el => el && el.offsetParent !== null;
        const tab = document.querySelector('#deviceWsTabs .config-main-tab.active');
        const grp = document.querySelector('#deviceWsGroups .active');
        (shown(tab) ? tab : shown(grp) ? grp : document.getElementById('ddvActions'))?.focus({ preventScroll: true });
    },

    // fields of the open editor the user changed (a trusted input) and that
    // differ from what they were drawn with — a switch that applies at once
    // (data-action) and a table's ticks are saved already
    _ddvEditDirty() {
        if (this._ddvFullDirty()) return true;           // a full editor's staged edits
        const panel = document.querySelector('#deviceDetailView [data-dpanel].ddv-editing');
        if (!panel || !this._ddvEdited || !this._ddvEdited.size) return false;
        for (const id of this._ddvEdited) {
            const el = document.getElementById(id);
            if (!el || !panel.contains(el) || el.disabled) continue;
            if (el.type === 'checkbox' || el.type === 'radio') {
                if (el.hasAttribute('data-action') || el.closest('table')) continue;
                if (el.checked !== el.defaultChecked) return true;
            } else if (el.tagName === 'SELECT') {
                const opts = [...el.options];
                const dflt = opts.some(o => o.defaultSelected) ? opts.findIndex(o => o.defaultSelected) : 0;
                if (el.selectedIndex !== dflt) return true;
            } else if (el.value !== el.defaultValue) return true;
        }
        return false;
    },

    // a link that promises an edit: the editor for an admin, Setup (at that
    // section) for the others
    ddvGoEdit(key, section) {
        if (window.DeviceViewModel.allows(this._effRole(), 'admin')) return this.ddvEdit(key);
        return this._switchDeviceTab('setup', { section, focusSection: true });
    },

    // E opens Edit… where the page is (never in a field, a dialog or a menu)
    _ddvWireKeys() {
        if (this._ddvKeysWired) return;
        this._ddvKeysWired = true;
        // leaving the page with a full editor's staged edits: the browser asks
        window.addEventListener('beforeunload', ev => {
            if (this._ddvFullDirty && this._ddvFullDirty()) { ev.preventDefault(); ev.returnValue = ''; }
        });
        document.addEventListener('keydown', ev => {
            // Esc in a full editor is Done: it asks about what is staged
            // (never under a dialog, a menu or a picker of its own)
            if (ev.key === 'Escape' && !ev.defaultPrevented
                && document.getElementById('deviceDetailView')?.classList.contains('ddv-full')
                && !document.querySelector('.modal.active') && !ev.target?.closest?.('.mbg-menu, .cbx-pop')) {
                ev.preventDefault();
                this.ddvEditDone();
                return;
            }
            if (ev.key !== 'e' && ev.key !== 'E') return;
            if (ev.repeat || ev.isComposing || ev.ctrlKey || ev.metaKey || ev.altKey || ev.defaultPrevented) return;
            const el = ev.target;
            if (el && (el.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.closest?.('.mbg-menu, .cbx-pop'))) return;
            if (this.currentPage !== 'devices' || this._view?.type !== 'device' || !this._devDetail) return;
            if (document.querySelector('.modal.active')) return;
            const view = document.getElementById('deviceDetailView');
            if (!view || view.style.display === 'none') return;
            const M = window.DeviceViewModel;
            if (!M.allows(this._effRole(), 'admin')) return;
            ev.preventDefault();
            // the editor of what is in view: the item the menu labels "E"
            const s = this._devDetail;
            this.ddvEdit(M.editKeyFor({ ...(s.entry || {}), id: s.id }, this._ddvEditing ? (this._ddvEditReturn || {}) : this._ddvViewState()));
        });
    },

    // ── the Setup tab (read-only) ────────────────────────────────────────
    _connDefaultsGet() {
        if (!this._connDefaults) {
            this._connDefaults = fetch('/api/protocols').then(r => (r.ok ? r.json() : {}))
                .then(d => d.defaults || {}).catch(() => ({}));
        }
        return this._connDefaults;
    },

    async _renderSetup(section, focus = false) {
        const host = document.getElementById('ddvSetup');
        const s = this._devDetail;
        if (!host || !s) return;
        const M = window.DeviceViewModel;
        const t = (k, d, p) => this.t(k, d, p);
        const defaults = await this._connDefaultsGet();
        if (this._devDetail !== s) return;
        const e = { ...(s.entry || {}), id: s.id };
        const secs = M.setupSections(e, defaults, t);
        const dflt = t('ddv.setup.default', 'default');
        // each output's live state, as its editor says it (on, off here, off in
        // Settings, not connected, idle)
        const live = key => {
            const sink = (e.sinks || {})[key];
            return sink && 'enabled' in sink ? ` ${this._sinkStatusPill(sink, !!sink.enabled, e.connected)}` : '';
        };
        host.innerHTML = `<p class="field-hint setup-intro"><i aria-hidden="true" class="bi bi-info-circle"></i> ${this._esc(this._roleHint('ddv.setup.intro', 'What this device is told, as stored. It is changed from ⋯ → Edit device…; the E key opens the part in view.', 'ddv.setup.introOther', 'What this device is told, as stored. An admin changes it.'))}</p>` + secs.map(sec => `
            <section class="settings-card setup-sec" data-setup="${sec.id}" aria-labelledby="setupH-${sec.id}">
                <div class="settings-card-header"><h3 id="setupH-${sec.id}" tabindex="-1">${this._esc(sec.title)}</h3></div>
                <div class="settings-card-body">
                    <dl class="setup-dl">${sec.rows.map(r => `<div class="setup-row" data-setup-row="${this._esc(r.id)}"><dt>${this._esc(r.label)}</dt>
                        <dd>${this._esc(r.value)}${r.note ? ` <span class="setup-default">(${this._esc(dflt)})</span>` : ''}${sec.id === 'outputs' ? live(r.id) : ''}</dd></div>`).join('')}</dl>
                    ${sec.id === 'outputs' && ((e.sinks || {}).rest || {}).enabled ? `<div class="setup-rest">
                        <button data-live type="button" class="btn btn-ghost btn-sm" ${this._act('ddvRestTest', [s.id])}><i aria-hidden="true" class="bi bi-send"></i> ${this._esc(t('rest.test', 'Test push'))}</button>
                        <span class="rest-status" id="setupRestStatus" aria-live="polite"></span></div>` : ''}
                    ${sec.id === 'pq' ? '<div class="rest-status" data-pq-status aria-live="polite"></div>' : ''}
                    ${sec.id === 'meters' ? '<div class="setup-gives"></div>' : ''}
                    ${sec.id === 'polling' ? '<div class="setup-pg" aria-live="off"></div>' : ''}
                    ${(sec.locks || []).length ? `<div class="setup-locks"><h4>${this._esc(t('ddv.setup.locks', 'Fixed, and why'))}</h4>
                        <ul>${sec.locks.map(l => `<li data-lock="${this._esc(l.code)}"><code>${this._esc(l.field)}</code> — ${this._esc(this._lockText(l))}</li>`).join('')}</ul></div>` : ''}
                    ${sec.note ? `<p class="field-hint"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(sec.note)}</p>` : ''}
                    ${sec.id === 'identity' ? '<div class="setup-attr"></div>' : ''}
                </div>
            </section>`).join('');
        this._setupSection = section || '';
        if (section && focus) {
            const h = document.getElementById(`setupH-${section}`) || document.getElementById('setupH-connection');
            this._setupFocusId = h?.id || '';
            h?.scrollIntoView({ block: 'start' });
            h?.focus({ preventScroll: true });
        }
        host.querySelectorAll('section[data-setup]').forEach(sec => sec.addEventListener('focusin', () => {
            this._setupSection = sec.dataset.setup;
        }));
        this._setupPollGroups(s.id, host.querySelector('.setup-pg'));
        this._setupAttributes(s.id, host.querySelector('.setup-attr'));
        if (host.querySelector('[data-pq-status]')) this._loadPqSinkStatus(s.id);
        this._setupGives(e, host.querySelector('.setup-gives'));
    },

    // what the meters give it, and why a value is not published — with the
    // live powers, as the meters editor says it
    async _setupGives(me, box) {
        if (!box || !(me.meters || []).length) return;
        let live = {};
        try {
            const d = await (await fetch('/api/values?device=' + encodeURIComponent(me.id))).json();
            Object.values(d.values || {}).forEach(v => { live[v.name] = v.value; });
        } catch (e) { live = {}; }
        if (this._devDetail?.id !== me.id || !box.isConnected) return;
        box.innerHTML = this._metersGivesHtml(me, live).replace(/<h3\b/g, '<h4').replace(/<\/h3>/g, '</h4>')
            .replace(/ id="mtrFlow"/g, '');
    },

    // Test push (a live action): the saved REST push, tried now; nothing is saved
    async ddvRestTest(id) {
        const box = document.getElementById('setupRestStatus');
        try {
            const r = await window.DeviceStore.devWrite(id, 'POST', `/api/devices/${encodeURIComponent(id)}/rest-push/test`);
            const d = await r.json().catch(() => ({}));
            const ok = r.ok && d.ok;
            const said = ok ? `✓ ${this.t('rest.ok', 'push OK')}${d.code ? ` (${d.code})` : ''}` : `✕ ${d.error || this._respError(r, d)}`;
            if (box) box.textContent = said;
            this.showToast(ok ? 'success' : 'error', this.t('rest.title', 'REST push'), said);
        } catch (e) { this.showToast('error', this.t('common.error', 'Error'), e.message); }
    },

    // the Setup section a link asked for, while it is shown (the router
    // ends a navigation there)
    _ddvFocusTarget() {
        const h = this._setupFocusId && document.getElementById(this._setupFocusId);
        return h && !h.closest('[hidden]') ? h : null;
    },

    async _setupPollGroups(id, box) {
        if (!box || !id) return;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/poll-groups`);
            if (!r.ok) return;
            const groups = (await r.json()).poll_groups || {};
            if (this._devDetail?.id !== id) return;
            const rows = Object.entries(groups);
            if (!rows.length) return;
            box.innerHTML = `<h4>${this._esc(this.t('devices.pollGroups', 'Poll-group intervals (seconds)'))}</h4>
                <dl class="setup-dl">${rows.map(([name, g]) => `<div class="setup-row"><dt>${this._esc(name)}</dt>
                    <dd>${this._esc(g.interval)} s${g.description ? ` <span class="field-hint">${this._esc(g.description)}</span>` : ''}</dd></div>`).join('')}</dl>`;
        } catch (e) { /* the intervals are a convenience here */ }
    },

    async _setupAttributes(id, box) {
        if (!box || !id) return;
        try {
            const [catalog, r] = await Promise.all([this._attrCatalogGet(), fetch(`/api/attributes/devices/${encodeURIComponent(id)}`)]);
            if (!r.ok) return;
            const data = await r.json();
            if (this._devDetail?.id !== id) return;
            const held = Object.keys(data.attributes || {});
            const cats = (catalog || {}).categories || {};
            box.innerHTML = `<h4>${this._esc(this.t('attr.title', 'Attributes'))}</h4>`
                + (held.length ? held.map(c => this._attrCatHtml(c, data.attributes[c], cats[c])).join('')
                    : `<p class="field-hint">${this._esc(this.t('attr.none', 'No attributes yet.'))}</p>`);
        } catch (e) { /* the attributes are a convenience here */ }
    },

    // ── operating ────────────────────────────────────────────────────────
    // Write…: a value to this device (only when it is not write-locked)
    ddvWrite(id) {
        this.openWriteModal(id);
    },

    // an enabled device that does not run: its settings sent again
    // — what is stored, never what a form holds: typed edits are asked about
    // first (and dropped), the PUT carries the device as it was opened
    async ddvReconnect(id) {
        if (!(this._view?.type === 'device' && this._devDetail?.id === id) && !await this.openDeviceDetail(id)) return;
        if (this._ddvEditDirty() && !await this._confirmDiscard()) return;
        const s = this._devDetail;
        const r = await window.DeviceStore.devWrite(s.id, 'PUT', `/api/devices/${encodeURIComponent(s.id)}`, this._ddvPayload(s.id, s.orig));
        const res = await r.json().catch(() => ({}));
        if (!r.ok) {
            if (r.status !== 412) this.showToast('error', this.t('devices.reconnect', 'Reconnect'), this._respError(r, res));
            return;
        }
        this.showToast('success', this.t('devices.reconnect', 'Reconnect'),
                       res.restarted === false ? this.t('devices.savedKept', 'Saved — kept running') : this.t('devices.savedRestarted', 'Saved — restarted'));
        await this._ddvRedraw(s.id);
    },

    // Unlock writes… asks first (what becomes writable, the master switch);
    // Lock acts at once (the safe direction). Never part of an edit.
    async ddvSetWriteLock(id, lock) {
        const t = (k, d, p) => this.t(k, d, p);
        const onPage = this._view?.type === 'device' && this._devDetail?.id === id;
        const e = (onPage ? this._devDetail.entry : null) || (this._devices || []).find(x => x.id === id) || { id };
        if (!lock) {
            const why = e.primary
                ? t('devices.writeLock.primaryHint', 'The primary device ships locked (security.primary_write_locked). Unlock only deliberately — this is the meter your live systems read.')
                : e.endpoint_id
                ? t('devices.writeLock.endpointHint', 'Installation-level lock — locking/unlocking here applies to EVERY unit of the installation (one physical connection). The global master switch must also be on for any write.')
                : t('devices.writeLock.hint', 'Locked: every Modbus write to this device is refused, declared or not. A new device starts locked; an admin unlocks it here. The global master switch (Settings → Security → Modbus writes) must also be on for any write.');
            const ok = await this.confirmAction({
                title: t('ddv.unlockTitle', 'Unlock writes on {name}?', { name: e.name || id }),
                message: why, confirmLabel: t('ddv.menu.unlockGo', 'Unlock writes'), danger: true,
            });
            if (!ok) return;
        }
        const r = await window.DeviceStore.devWrite(id, 'POST', `/api/devices/${encodeURIComponent(id)}/write-lock`, { locked: !!lock });
        const res = await r.json().catch(() => ({}));
        if (!r.ok) {
            if (r.status === 412) return;              // the store's banner says it
            this.showToast('error', t('devices.writeLock.title', 'Write protection'), this._respError(r, res));
            return;
        }
        this.showToast('success', t('devices.writeLock.title', 'Write protection'),
                       lock ? t('devices.writeLock.nowLocked', 'Device locked — writes refused')
                            : t('devices.writeLock.nowUnlocked', 'Device unlocked — writes allowed (master switch permitting)'));
        await this._ddvRedraw(id);
    },

    // the device drawn again from what is stored now, where it was
    async _ddvRedraw(id) {
        if (!(this._view?.type === 'device' && this._devDetail?.id === id)) return this.renderDevicesList();
        const was = this._ddvViewState();
        const editing = this._ddvEditing;
        if (!await this.openDeviceDetail(id)) return;
        if (editing) this.ddvEdit(editing);
        else if (was.tab !== 'overview') this._switchDeviceTab(was.tab, { section: was.section });
        const btn = document.getElementById('ddvActions');
        btn?.focus({ preventScroll: true });
    },
});
