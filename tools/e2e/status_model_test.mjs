// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// What the Status page calls a problem (ui/js/status-model.js), with no
// browser: night, standby, a cold start, rules, folding, outputs, config.
//
//   node tools/e2e/status_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const M = require('../../ui/js/status-model.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra ? ' — ' + JSON.stringify(extra) : ''}`);
};
const dev = (id, o = {}) => ({ id, name: id, enabled: true, health: 'ok', data_health: 'ok', connected: true, ...o });
const keys = m => m.issues.map(i => i.key);
const run = (snap, memo = {}) => M.assess({ now: 1000, ...snap }, memo);

// night: inverters the aggregator counted asleep are no problem, nor is their installation
{
  const m = run({ status: { devices: [dev('pv-u1', { endpoint_id: 'pv', health: 'down', asleep: true }), dev('pv-u2', { endpoint_id: 'pv', health: 'down', asleep: true }), dev('umg')],
                            endpoints: [{ id: 'pv', status: 'asleep', units_online: 0, units_total: 2, units_asleep: 2 }] } });
  check('night: no issue', m.issues.length === 0, keys(m));
  check('night: asleep counts online in the pill', m.pills.devices.n === 3 && m.pills.devices.asleep === 2, m.pills.devices);
  check('night: installation counts as up', m.pills.installations.n === 1);
}
// night without a location: the installation is offline, said once, with the hint; its units fold into it
{
  const m = run({ status: { devices: [dev('pv-u1', { endpoint_id: 'pv', health: 'down' }), dev('pv-u2', { endpoint_id: 'pv', health: 'down' })],
                            endpoints: [{ id: 'pv', name: 'PV', status: 'offline', units_online: 0, units_total: 2, units_asleep: null, inverters: true }] } });
  check('no location: one line for the installation', keys(m).join() === 'inst:pv', keys(m));
  check('no location: the hint', m.issues[0].what.k === 'status.why.instOfflineNoSun');
  check('no location: devices tab still marked', m.pills.devices.sev === 'error');
  const bank = run({ status: { devices: [], endpoints: [{ id: 'b', status: 'offline', units_total: 2, units_asleep: null, inverters: false }] } });
  check('no inverters: no sleep hint', bank.issues[0].what.k === 'status.why.instOffline');
}
// standby: nothing polls, nothing is red
{
  const m = run({ health: { role: { role: 'standby' } },
                  status: { devices: [dev('a', { health: 'down' }), dev('b')], endpoints: [{ id: 'pv', status: 'offline', units_total: 2 }] },
                  vm: [{ name: 'vm', port: 502, enabled: true, running: false }],
                  rules: [{ id: 'r', enabled: true, mode: 'armed', live: { state: 'stale', error: 'x' } }] });
  check('standby: no issues', m.issues.length === 0, keys(m));
  check('standby: flagged', m.standby === true);
  check('standby: rule word', m.rules[0].word === 'standby');
}
// a virtual meter waiting for the active gateway is not stopped
{
  const m = run({ status: { devices: [] }, vm: [{ name: 'vm', port: 502, enabled: true, running: false, waits_for_active: true }] });
  check('waits_for_active: no issue', m.issues.length === 0, keys(m));
}
// cold start: health (the Devices page word) wins over data_health
{
  const m = run({ status: { devices: [dev('a', { health: 'degraded', data_health: 'ok', connected: false })] } });
  check('cold start: degraded is a warning', m.issues.length === 1 && m.issues[0].sev === 'warn', m.issues);
}
// disabled devices and rules are never problems, whatever their last state
{
  const m = run({ status: { devices: [dev('a', { enabled: false, health: 'idle' })] },
                  rules: [{ id: 'r', enabled: false, mode: 'armed', live: { state: 'lowering', error: 'old' } }] });
  check('disabled: no issue', m.issues.length === 0, keys(m));
  check('disabled rule reads disabled', m.rules[0].word === 'disabled');
  check('disabled device out of the count', m.pills.devices.of === 0);
}
// rules: an error is an error when armed, a warning in shadow; stale only while its devices are awake
{
  const memo = {};
  const m = run({ status: { devices: [dev('pv-u1', { asleep: true, health: 'down' }), dev('inv2')] },
                  rules: [{ id: 'armed-err', enabled: true, mode: 'armed', live: { error: 'bad expr' } },
                          { id: 'shadow-err', enabled: true, mode: 'shadow', live: { error: 'bad expr' } },
                          { id: 'night', enabled: true, mode: 'armed', live: { state: 'stale', units: { 'pv-u1': {} } } },
                          { id: 'day', enabled: true, mode: 'armed', live: { state: 'stale', units: { 'inv2': {} } } },
                          { id: 'shadow-stale', enabled: true, mode: 'shadow', live: { state: 'stale', units: { 'inv2': {} } } }] }, memo);
  const sev = Object.fromEntries(m.issues.map(i => [i.key, i.sev]));
  check('armed rule error is an error', sev['rule:armed-err'] === 'error', sev);
  check('shadow rule error is a warning', sev['rule:shadow-err'] === 'warn', sev);
  check('stale on sleeping inverters is quiet', !('rule:night' in sev), sev);
  check('stale by day is a warning', sev['rule:day'] === 'warn', sev);
  check('shadow stale is quiet', !('rule:shadow-stale' in sev), sev);
  check('errors before warnings', m.issues[0].sev === 'error');
}
// rules reading what is not here: an armed one reading a missing device is
// red in one row (not beside its stale one); a missing field, or a shadow
// rule, is amber
{
  const gone = { in: 'signal', ref: 'nosuch.x', problem: 'no_device' };
  const field = { in: 'signal', ref: 'pv-u1.voltage_l1', problem: 'no_field', hint: 'voltage_l1_n' };
  const m = run({ status: { devices: [dev('pv-u1')] },
                  rules: [{ id: 'armed-gone', enabled: true, mode: 'armed', inputs: [gone], live: { state: 'stale', units: { 'pv-u1': {} } } },
                          { id: 'armed-field', enabled: true, mode: 'armed', inputs: [field], live: { state: 'normal' } },
                          { id: 'shadow-gone', enabled: true, mode: 'shadow', inputs: [gone], live: { state: 'stale' } },
                          { id: 'off-gone', enabled: false, mode: 'armed', inputs: [gone], live: {} }] });
  const sev = Object.fromEntries(m.issues.map(i => [i.key, i.sev]));
  check('inputs: armed rule reading a missing device is an error', sev['rule:armed-gone'] === 'error', sev);
  check('inputs: one row for it, not a second beside stale', keys(m).filter(k => k === 'rule:armed-gone').length === 1, keys(m));
  check('inputs: names what it reads', m.issues.find(i => i.key === 'rule:armed-gone').what.k === 'status.why.ruleInputsGone');
  check('inputs: a missing field is a warning', sev['rule:armed-field'] === 'warn', sev);
  check('inputs: a shadow rule is a warning', sev['rule:shadow-gone'] === 'warn', sev);
  check('inputs: a disabled rule is quiet', !('rule:off-gone' in sev), sev);
}
// rtu_tap stale is a warning; down an error
{
  const m = run({ status: { devices: [dev('tap', { health: 'stale' }), dev('x', { health: 'down' })] } });
  check('stale warns, down errs', m.issues.map(i => i.sev).join() === 'error,warn', m.issues);
}
// a bridge offline folds the devices behind it — unless they all sleep
{
  const devices = [dev('h1', { health: 'down' }), dev('h2', { health: 'down' }), dev('other', { health: 'down' })];
  const bridges = [{ id: 'br', name: 'Bridge', state: { status: 'offline' }, ports: [{ port: 1, devices: [{ id: 'h1' }, { id: 'h2' }] }] }];
  const m = run({ status: { devices }, bridges });
  check('bridge: one line + the unrelated device', keys(m).sort().join() === 'bridge:br,dev:other', keys(m));
  check('bridge: counts its down devices', m.issues.find(i => i.key === 'bridge:br').what.p.n === 2);
  const asleep = [dev('h1', { asleep: true, health: 'down' }), dev('h2', { asleep: true, health: 'down' })];
  check('bridge of sleeping devices: quiet', run({ status: { devices: asleep }, bridges }).issues.length === 0);
}
// every device of one MQTT input broker down: the broker, once
{
  const devices = [dev('m1', { health: 'down', mqtt_in_broker: 'user:pw@192.0.2.9' }), dev('m2', { health: 'down', mqtt_in_broker: 'user:pw@192.0.2.9' })];
  const m = run({ status: { devices } });
  check('broker: folded into one line', keys(m).join() === 'broker:user:pw@192.0.2.9', keys(m));
  check('broker: no credentials in the name', m.issues[0].name === '192.0.2.9', m.issues[0].name);
}
// latency: one slow answer is noise, two in a row a warning
{
  const memo = {};
  const snap = sample => ({ sample, status: { devices: [dev('a', { last_latency_ms: 2500, read_via: [{ timeout_s: 3 }] })] } });
  check('latency: first slow poll is quiet', run(snap(1), memo).issues.length === 0);
  check('latency: a repaint of the same poll is no second sample', run(snap(1), memo).issues.length === 0);
  const m = run(snap(2), memo);
  check('latency: second is a warning', m.issues.length === 1 && m.issues[0].what.k === 'status.why.slow', m.issues);
  const alerts = { status: { thresholds: { latency_ms: 500 } } };
  const memo2 = {};
  const s2 = sample => ({ sample, status: { devices: [dev('b', { last_latency_ms: 800 })] }, alerts });
  run(s2(1), memo2);
  check('latency: alerts threshold for a standalone device', run(s2(2), memo2).issues.length === 1);
}
// outputs: InfluxDB judged on confirmations, auth and drops
{
  const base = { enabled: true, connected: true };
  const k = influx => keys(run({ status: { devices: [], influxdb: { ...base, ...influx } } })).join();
  check('influx: auth failed, one line', k({ auth_failed: true, connected: false }) === 'influx.auth');
  check('influx: never confirmed since start errs', keys(run({ status: { devices: [], influxdb: { ...base, buffer_points: 9, last_confirm_age_s: null } }, res: { uptime_s: 900 } })).join() === 'influx.silent');
  check('influx: buffering warns', k({ buffer_points: 40, last_confirm_age_s: 5 }) === 'influx.buffer');
  check('influx: silent too long errs', k({ buffer_points: 40, last_confirm_age_s: 900 }) === 'influx.silent');
  check('influx: dropped is persistent', k({ dropped_total: 7 }) === 'influx.dropped');
  const memo = {};
  run({ status: { devices: [], mqtt: { enabled: true, connected: true, messages_failed: 1 } } }, memo);
  const m = M.assess({ now: 1002, status: { devices: [], mqtt: { enabled: true, connected: true, messages_failed: 3 } } }, memo);
  check('mqtt: failures growing warn', keys(m).join() === 'mqtt.loss', keys(m));
  const later = M.assess({ now: 1100, status: { devices: [], mqtt: { enabled: true, connected: true, messages_failed: 3 } } }, memo);
  check('mqtt: and stop warning once they stop', later.issues.length === 0, keys(later));
}
// OPC UA: a grace period after start; a 503 is not a failure
{
  const op = { enabled: true, running: false, error: '' };
  check('opcua: grace at start', run({ status: { devices: [] }, opcua: op, res: { uptime_s: 10 } }).issues.length === 0);
  check('opcua: error later', keys(run({ status: { devices: [] }, opcua: op, res: { uptime_s: 120 } })).join() === 'opcua');
  check('opcua: unknown uptime is still the grace', run({ status: { devices: [] }, opcua: op }).issues.length === 0);
  check('opcua: unavailable is quiet', run({ status: { devices: [] }, opcua: { unavailable: true } }).issues.length === 0);
}
// configuration
{
  check('config: saves disabled errs', run({ status: { devices: [], config: { saves_disabled: true } } }).issues[0].sev === 'error');
  check('config: healed warns', run({ status: { devices: [], config: { healed_from_snapshot: true } } }).issues[0].sev === 'warn');
}
// a sequence aborted within the hour
{
  const seq = ended => [{ id: 's', last: [{ status: 'aborted', ended }] }];
  check('sequence: recent abort warns', run({ status: { devices: [] }, sequences: seq(900) }).issues.length === 1);
  check('sequence: old abort quiet', run({ status: { devices: [] }, sequences: seq(-5000) }).issues.length === 0);
}
// "since": the device's own time in state, else when the page first saw it
{
  const memo = {};
  const snap = now => M.assess({ now, status: { devices: [dev('a', { health: 'down', up_since_s: 240 })], mqtt: { enabled: true, connected: false } } }, memo);
  snap(1000);
  const m = snap(1030);
  const byKey = Object.fromEntries(m.issues.map(i => [i.key, i]));
  check('since: device uses up_since_s', byKey['dev:a'].since === 240 && !byKey['dev:a'].seen);
  check('since: others are seen for 30 s', byKey.mqtt.since === 30 && byKey.mqtt.seen === true, byKey.mqtt);
  check('since: older first within a severity', m.issues[0].key === 'dev:a');
}
// levels and the titlebar count
{
  check('levels', ['warn', 'warning', 'error', 'critical', 'info', ''].map(M.normLevel).join() === 'warn,warn,error,error,info,info');
  const c = M.onlineCount([dev('a'), dev('b', { health: 'down' }), dev('c', { enabled: false }), dev('d', { asleep: true })]);
  check('titlebar: answering or asleep, of the enabled', c.online === 2 && c.total === 3, c);
}

console.log(`${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
