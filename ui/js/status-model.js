/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// What needs attention, judged once. Pure: no
// DOM, no fetch — the Status page, its pills, its tab markers, its attention
// list and the titlebar count all read this, and Node tests it directly.
(function (root) {
    'use strict';

    const RANK = { error: 3, warn: 2, info: 1, ok: 0 };
    const worst = (a, b) => ((RANK[a] || 0) >= (RANK[b] || 0) ? a : b);
    const TABS = ['devices', 'installations', 'outputs', 'automation', 'gateway'];
    const WORD_SEV = { down: 'error', degraded: 'warn', stale: 'warn' };
    const SEQ_RECENT_S = 3600;          // an aborted sequence matters for an hour
    const GROWTH_HOLD_S = 60;           // a counter that grew stays a warning this long
    const OPCUA_GRACE_S = 30;           // OPC UA starts after the web server
    const INFLUX_SILENT_S = 300;        // queued points and nothing confirmed for 5 min

    // event and alert levels arrive as info / warn / warning / error / critical
    function normLevel(l) {
        l = String(l || '').toLowerCase();
        if (l === 'error' || l === 'critical') return 'error';
        if (l === 'warn' || l === 'warning') return 'warn';
        return 'info';
    }

    // one word per device: disabled, asleep, standby, or its health
    function deviceWord(d, standby) {
        if (d.enabled === false) return 'disabled';
        if (d.asleep) return 'asleep';
        if (standby) return 'standby';
        return d.health || d.data_health || 'idle';
    }

    // the titlebar's "online / total": enabled devices answering, or asleep
    function onlineCount(devices, standby) {
        const enabled = (devices || []).filter(d => d.enabled !== false);
        // a standby polls nothing on purpose: count its devices as accounted for
        const on = enabled.filter(d => ['ok', 'degraded', 'asleep', 'standby'].includes(deviceWord(d, standby)));
        return { online: on.length, total: enabled.length };
    }

    const brokerHost = b => String(b || '').replace(/^[a-z]+:\/\//i, '').replace(/^.*@/, '');

    // a counter that grew since the last poll, remembered for GROWTH_HOLD_S
    function grew(memo, key, value, now) {
        memo.cnt = memo.cnt || {};
        memo.grewAt = memo.grewAt || {};
        const prev = memo.cnt[key];
        memo.cnt[key] = value;
        if (typeof value === 'number' && typeof prev === 'number' && value > prev) memo.grewAt[key] = now;
        const at = memo.grewAt[key];
        return at != null && now - at < GROWTH_HOLD_S;
    }

    /**
     * snap: {now (s), status (/api/status), health (/health), vm (instances),
     *        res, rules, sequences, bridges, opcua ({unavailable:true} on 503),
     *        alerts (/api/alerts), events}
     * memo: an object kept between calls (latency streaks, counters, first seen)
     */
    function assess(snap, memo) {
        memo = memo || {};
        const now = snap.now || Date.now() / 1000;
        const st = snap.status || {};
        const roleInfo = (snap.health && snap.health.role) || {};
        const standby = roleInfo.role === 'standby';
        const issues = [];
        const add = (sev, tab, key, name, what, extra) =>
            issues.push(Object.assign({ sev, tab, key, name, what }, extra || {}));
        const tabSev = { devices: 'ok', installations: 'ok', outputs: 'ok', automation: 'ok', gateway: 'ok' };
        const mark = (tab, sev) => { tabSev[tab] = worst(tabSev[tab], sev); };

        // ── devices ──
        memo.lat = memo.lat || {};
        // a streak counts polls, not paints: a search keystroke repaints too
        const newSample = snap.sample == null || snap.sample !== memo.latSample;
        memo.latSample = snap.sample;
        const latLimit = Number((((snap.alerts || {}).status || {}).thresholds || {}).latency_ms) || 0;
        const devices = (st.devices || []).map(d => {
            const word = deviceWord(d, standby);
            let over = false;
            if ((word === 'ok' || word === 'degraded') && d.last_latency_ms != null) {
                const tmo = Math.min(...(d.read_via || []).map(r => Number(r.timeout_s)).filter(x => x > 0));
                const lim = isFinite(tmo) ? tmo * 500 : latLimit > 0 ? latLimit : null;
                over = lim != null && d.last_latency_ms > lim;
            }
            if (newSample) memo.lat[d.id] = over ? (memo.lat[d.id] || 0) + 1 : 0;
            const slow = memo.lat[d.id] >= 2;              // one slow answer is noise
            const sev = WORD_SEV[word] || (slow ? 'warn' : 'ok');
            return Object.assign({}, d, { word, slow, sev });
        });
        const byId = new Map(devices.map(d => [d.id, d]));
        Object.keys(memo.lat).forEach(id => { if (!byId.has(id)) delete memo.lat[id]; });
        const folded = new Set();
        const asleepAll = ids => ids.length > 0 && ids.every(id => (byId.get(id) || {}).word === 'asleep');

        // a bridge offline explains the devices behind it
        const bridges = (snap.bridges || []).map(b => {
            const ids = [];
            (b.ports || []).forEach(p => (p.devices || []).forEach(x => ids.push(x.id)));
            const enabledIds = ids.filter(id => byId.has(id) && byId.get(id).enabled !== false);
            const status = (b.state || {}).status || 'unknown';
            // a bridge whose devices all sleep is offline by night, not broken
            const quiet = standby || asleepAll(enabledIds);
            const sev = quiet ? 'ok' : status === 'offline' ? 'error' : status === 'degraded' ? 'warn' : 'ok';
            return Object.assign({}, b, { ids: enabledIds, sev, quiet });
        });
        if (!standby) {
            bridges.forEach(b => {
                if (b.sev === 'ok') return;
                const down = b.ids.filter(id => byId.get(id).sev === 'error');
                if (b.sev === 'error') down.forEach(id => folded.add(id));
                add(b.sev, 'gateway', 'bridge:' + b.id, b.name || b.id,
                    { k: b.sev === 'error' ? 'status.why.bridgeOffline' : 'status.why.bridgeDegraded',
                      d: b.sev === 'error' ? 'bridge offline · {n} device(s) behind it' : 'bridge degraded',
                      p: { n: down.length } },
                    { open: { method: 'openBridge', args: [b.id] } });
                mark('gateway', b.sev);
            });

            // every device of one MQTT input broker down: the broker, once
            const byBroker = {};
            devices.forEach(d => {
                if (d.enabled === false || !d.mqtt_in_broker) return;
                (byBroker[d.mqtt_in_broker] = byBroker[d.mqtt_in_broker] || []).push(d);
            });
            Object.entries(byBroker).forEach(([broker, list]) => {
                const live = list.filter(d => d.word !== 'asleep');
                if (live.length < 2 || !live.every(d => d.word === 'down')) return;
                live.forEach(d => folded.add(d.id));
                add('error', 'devices', 'broker:' + broker, brokerHost(broker),
                    { k: 'status.why.brokerDown', d: 'MQTT broker unreachable · {n} device(s)', p: { n: live.length } },
                    { open: { method: 'statusTab', args: ['devices'] } });
            });
        }

        // ── installations ──
        const installations = (st.endpoints || []).map(e => {
            let word = e.enabled === false ? 'disabled' : standby ? 'standby' : (e.status || 'unknown');
            const sev = word === 'offline' ? 'error' : word === 'partial' ? 'warn' : 'ok';
            return Object.assign({}, e, { word, sev });
        });
        installations.forEach(e => {
            if (e.sev === 'ok') return;
            if (e.sev === 'error') devices.forEach(d => { if (d.endpoint_id === e.id && d.sev === 'error') folded.add(d.id); });
            const noSun = e.units_asleep == null && e.inverters !== false;   // no location: no inverter can sleep
            add(e.sev, 'installations', 'inst:' + e.id, e.name || e.id,
                e.sev === 'error'
                    ? (noSun ? { k: 'status.why.instOfflineNoSun', d: 'offline · 0/{n} units — at night? set the plant location so inverters can sleep', p: { n: e.units_total || 0 } }
                             : { k: 'status.why.instOffline', d: 'offline · 0/{n} units', p: { n: e.units_total || 0 } })
                    : { k: 'status.why.instPartial', d: 'partial · {on}/{n} units', p: { on: e.units_online || 0, n: e.units_total || 0 } },
                { open: { method: 'openEndpointDetail', args: [e.id] } });
            mark('installations', e.sev);
        });

        devices.forEach(d => {
            if (d.sev === 'ok') return;
            mark('devices', d.sev);
            if (folded.has(d.id)) return;
            const fails = Object.entries(d.error_counts || {}).sort((a, b) => b[1] - a[1])
                .slice(0, 2).map(([k, n]) => `${k.replace(/^exception_/, 'exc ')} ${n}`).join(' · ');
            const what = d.word === 'ok'                     // ok and slow: the latency is the news
                ? { k: 'status.why.slow', d: 'slow · {ms} ms', p: { ms: Math.round(d.last_latency_ms) } }
                : { k: 'status.word.' + d.word, d: d.word, extra: fails };
            add(d.sev, 'devices', 'dev:' + d.id, d.name || d.id, what,
                { since: d.up_since_s, open: { method: 'openDeviceDetail', args: [d.id] } });
        });

        // ── outputs ──
        const mqtt = st.mqtt || {}, influx = st.influxdb || {};
        if (mqtt.enabled && !mqtt.connected) {
            add('error', 'outputs', 'mqtt', 'MQTT', { k: 'status.why.disconnected', d: 'disconnected' },
                { since: mqtt.disconnected_for_s, open: { method: 'statusTab', args: ['outputs'] } });
            mark('outputs', 'error');
        }
        if (mqtt.enabled) {
            const g1 = grew(memo, 'mqtt.failed', mqtt.messages_failed, now);
            const g2 = grew(memo, 'mqtt.drops', mqtt.publish_drops, now);
            if (g1 || g2) {
                add('warn', 'outputs', 'mqtt.loss', 'MQTT', { k: 'status.why.mqttLoss', d: 'messages failing or dropped' },
                    { open: { method: 'statusTab', args: ['outputs'] } });
                mark('outputs', 'warn');
            }
        }
        if (influx.enabled) {
            const iss = [];
            if (!influx.connected && !influx.auth_failed) iss.push(['error', 'influx', { k: 'status.why.disconnected', d: 'disconnected' }]);
            if (influx.auth_failed) iss.push(['error', 'influx.auth', { k: 'status.why.authFailed', d: 'the token is refused' }]);
            const buffered = influx.buffer_points || 0;
            // never confirmed at all: silent since the gateway started
            const silent = influx.last_confirm_age_s != null ? influx.last_confirm_age_s
                : (snap.res && snap.res.uptime_s) || 0;
            if (buffered > 0 && silent > INFLUX_SILENT_S)
                iss.push(['error', 'influx.silent', { k: 'status.why.influxSilent', d: '{n} points queued, nothing confirmed for {m} min', p: { n: buffered, m: Math.round(silent / 60) } }]);
            else if (buffered > 0)
                iss.push(['warn', 'influx.buffer', { k: 'status.why.buffering', d: 'buffering · {n} points', p: { n: buffered } }]);
            if ((influx.dropped_total || 0) > 0)
                iss.push(['warn', 'influx.dropped', { k: 'status.why.dropped', d: '{n} points dropped since start', p: { n: influx.dropped_total } }]);
            if (grew(memo, 'influx.failed', influx.writes_failed, now))
                iss.push(['warn', 'influx.failed', { k: 'status.why.writesFailing', d: 'writes failing' }]);
            iss.forEach(([sev, key, what]) => {
                add(sev, 'outputs', key, 'InfluxDB', what, { open: { method: 'statusTab', args: ['outputs'] } });
                mark('outputs', sev);
            });
        }
        const vms = snap.vm || [];
        if (!standby) {
            vms.forEach(i => {
                if (!i.enabled || i.running || i.waits_for_active) return;
                add('error', 'outputs', 'vm:' + (i.name || i.template) + ':' + i.port, (i.name || i.template),
                    { k: 'status.why.stopped', d: 'stopped' }, { open: { method: 'navigateTo', args: ['vmeters'] } });
                mark('outputs', 'error');
            });
        }
        const op = snap.opcua;
        const uptime = snap.res && snap.res.uptime_s != null ? snap.res.uptime_s : 0;   // unknown: still starting
        if (op && !op.unavailable && op.enabled && !op.running && uptime > OPCUA_GRACE_S) {
            add('error', 'outputs', 'opcua', 'OPC UA', { k: 'status.why.opcuaDown', d: 'enabled, not running', extra: op.error || '' },
                { open: { method: 'statusTab', args: ['outputs'] } });
            mark('outputs', 'error');
        }

        // ── automation ──
        const rules = (snap.rules || []).map(r => {
            const live = r.live || {};
            const armed = r.mode === 'armed';
            let word = live.state || 'unbound', sev = 'ok';
            if (r.enabled === false) { word = 'disabled'; }
            else if (standby) { word = 'standby'; }
            else if (live.error) { word = 'cannotRun'; sev = armed ? 'error' : 'warn'; }
            else if (armed && (r.inputs || []).some(p => p.problem === 'no_device')) {
                // an armed rule reading a device that is not here can only ever
                // be stale: a protection that does not protect (one row, not
                // a second beside the stale one)
                word = 'inputsGone'; sev = 'error';
            }
            else if ((r.inputs || []).length) { word = 'inputsUnknown'; sev = 'warn'; }
            else if (armed && live.state === 'stale') {
                // a rule on sleeping inverters is stale by night, not broken
                if (!asleepAll(Object.keys(live.units || {}))) sev = 'warn';
            }
            return Object.assign({}, r, { word, sev, armed });
        });
        const inputsText = r => (r.inputs || []).map(p => p.ref).join(', ');
        rules.forEach(r => {
            if (r.sev === 'ok') return;
            add(r.sev, 'automation', 'rule:' + r.id, r.label || r.id,
                r.word === 'cannotRun'
                    ? { k: 'status.why.ruleError', d: 'cannot run', extra: String((r.live || {}).error || '') }
                    : r.word === 'inputsGone'
                        ? { k: 'status.why.ruleInputsGone', d: 'armed, reads a device that is not here', extra: inputsText(r) }
                        : r.word === 'inputsUnknown'
                            ? { k: 'status.why.ruleInputs', d: 'reads names this gateway does not have', extra: inputsText(r) }
                            : { k: 'status.why.ruleStale', d: 'armed, its signal is stale' },
                { open: { method: 'openRule', args: [r.id] } });
            mark('automation', r.sev);
        });
        const sequences = snap.sequences || [];
        if (!standby) {
            sequences.forEach(s => {
                const last = s.running || (s.last || [])[0];
                if (!last || last.status !== 'aborted') return;
                if (now - (last.ended || last.started || 0) > SEQ_RECENT_S) return;
                add('warn', 'automation', 'seq:' + s.id, s.label || s.id,
                    { k: 'status.why.seqAborted', d: 'aborted', extra: last.reason || '' },
                    { open: { method: 'navigateTo', args: ['rules'] } });
                mark('automation', 'warn');
            });
        }

        // ── gateway ──
        const cfg = st.config || {};
        if (cfg.saves_disabled) {
            add('error', 'gateway', 'config', 'config.yaml', { k: 'status.why.savesDisabled', d: 'the configuration did not load — saving is refused' },
                { open: { method: 'statusTab', args: ['gateway'] } });
            mark('gateway', 'error');
        } else if (cfg.healed_from_snapshot || cfg.written_by_newer) {
            add('warn', 'gateway', 'config', 'config.yaml',
                cfg.healed_from_snapshot ? { k: 'status.why.healed', d: 'restored from a snapshot at start' }
                                         : { k: 'status.why.newer', d: 'written by a newer gateway version' },
                { open: { method: 'statusTab', args: ['gateway'] } });
            mark('gateway', 'warn');
        }
        if ((cfg.ignored_blocks || []).length) {
            add('warn', 'gateway', 'config:blocks', 'config.yaml',
                { k: 'status.why.ignoredBlocks', d: 'a device block could not be read and was ignored: {what}',
                  p: { what: cfg.ignored_blocks.map(b => b.where).join(', ') } },
                { open: { method: 'statusTab', args: ['gateway'] } });
            mark('gateway', 'warn');
        }

        // ── order and "since" ──
        memo.first = memo.first || {};
        const seenNow = new Set();
        issues.forEach(i => {
            seenNow.add(i.key);
            if (memo.first[i.key] == null) memo.first[i.key] = now;
            if (i.since == null) { i.since = Math.max(0, Math.round(now - memo.first[i.key])); i.seen = true; }
        });
        Object.keys(memo.first).forEach(k => { if (!seenNow.has(k)) delete memo.first[k]; });
        issues.sort((a, b) => (RANK[b.sev] - RANK[a.sev]) || ((b.since || 0) - (a.since || 0)) || String(a.name).localeCompare(String(b.name)));

        devices.sort((a, b) => (RANK[b.sev] - RANK[a.sev]) || String(a.name || a.id).localeCompare(String(b.name || b.id)));

        // ── the pills ──
        const oc = onlineCount(devices, standby);
        const asleepN = devices.filter(d => d.word === 'asleep').length;
        const instOn = installations.filter(e => ['online', 'asleep'].includes(e.word)).length;
        const instEnabled = installations.filter(e => e.word !== 'disabled').length;
        const outs = [
            ['MQTT', mqtt.enabled, mqtt.connected],
            ['InfluxDB', influx.enabled, influx.connected && !influx.auth_failed],
            ['OPC UA', op && !op.unavailable && op.enabled, op && op.running],
            ['vMeters', vms.some(i => i.enabled), vms.every(i => !i.enabled || i.running || i.waits_for_active)],
        ].filter(o => o[1]);
        const armedN = rules.filter(r => r.enabled !== false && r.armed).length;
        const shadowN = rules.filter(r => r.enabled !== false && !r.armed).length;
        const pills = {
            devices: { sev: tabSev.devices, n: oc.online, of: oc.total, asleep: asleepN },
            installations: { sev: tabSev.installations, n: instOn, of: instEnabled },
            outputs: { sev: tabSev.outputs, n: outs.filter(o => o[2]).length, of: outs.length },
            automation: { sev: tabSev.automation, armed: armedN, shadow: shadowN, seqRunning: sequences.filter(s => s.running).length },
            gateway: { sev: tabSev.gateway, cpu: snap.res && snap.res.cpu_pct, rss: snap.res && snap.res.rss_mb },
        };

        return {
            standby, role: roleInfo, issues, pills, devices, installations, rules, bridges,
            errors: issues.filter(i => i.sev === 'error').length,
            warnings: issues.filter(i => i.sev === 'warn').length,
        };
    }

    const api = { assess, deviceWord, onlineCount, normLevel, worst, RANK, TABS };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.StatusModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
