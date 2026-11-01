// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A command's binding and its channel's pace (ui/js/cmd-binding-model.js),
// with no browser: grouping by channel, what each form shows, what a save
// sends, and what it refuses (ADR-0019).
//
//   node tools/e2e/cmd_binding_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const M = require('../../ui/js/cmd-binding-model.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// listing entries as GET /api/devices/{id}/commands serves them
const gx = (name, o = {}) => ({
  name, from_template: true, params: { value: { min: -50000, max: 50000 } }, safe: {}, safe_from: null,
  template_safe: null, lease_s: 0, channel: name, channel_key: name,
  template_channel: { label: name + ' (flash)', min_interval_s: 60 }, channel_override: {},
  channel_settings: { min_interval_s: 60 }, min_interval_s: 60, template_min_interval_s: 60, max_age_s: 30, ...o });
const vfd = [
  { name: 'run', from_template: true, params: { value: {} }, safe: { value: 0 }, safe_from: 'template', template_safe: { value: 0 },
    lease_s: 0, channel: 'motor', channel_key: 'motor', template_channel: { label: 'Motor', min_off_s: 5, max_starts_per_h: 30 },
    channel_override: {} },
  { name: 'stop', from_template: true, alias: { command: 'run', params: { value: 0 } }, params: {}, channel: 'motor', channel_key: 'motor',
    template_channel: { label: 'Motor', min_off_s: 5, max_starts_per_h: 30 }, channel_override: {} },
  { name: 'reset_fault', from_template: true, params: {}, safe: {}, template_safe: null, channel: 'reset_fault', channel_key: 'reset_fault',
    template_channel: {}, channel_override: {} },
];

// grouping
{
  const chs = M.channels(vfd);
  check('channels: one per channel, in listed order', eq(chs.map(c => c.cid), ['motor', 'reset_fault']));
  check('channels: an alias sits on its target\'s', eq(chs[0].cmds.map(c => c.name), ['run', 'stop']));
  check('channels: the template\'s label', chs[0].label === 'Motor');
  const f = M.channelFields(chs[0]).map(x => `${x.k}${x.adv ? '*' : ''}=${x.tpl}`);
  check('fields: interval, max age, and only what the template\'s motor declares', eq(f, ['min_interval_s=0', 'max_age_s*=30', 'min_off_s*=5', 'max_starts_per_h*=30']), f);
  check('fields: a plain channel has interval and max age only', M.channelFields(chs[1]).length === 2);
}
// channel: what a save sends
{
  const ch = M.channels([gx('ess_setpoint')])[0];
  check('channel: untouched is not dirty', !M.readChannel(ch, {}).dirty);
  const r = M.readChannel(ch, { min_interval_s: '5' });
  check('channel: only the changed key is sent', eq(r.payload, { min_interval_s: 5 }) && r.dirty, r);
  check('channel: lower than the template is warned with its value', r.below && r.below.tpl === 60 && r.below.value === 5, r.below);
  check('channel: equal or higher is not', !M.readChannel(ch, { min_interval_s: '60' }).below && !M.readChannel(ch, { min_interval_s: '90' }).below);
  check('channel: out of range refused', M.readChannel(ch, { min_interval_s: '90000' }).errors.min_interval_s.code === 'range');
  check('channel: not a number refused', M.readChannel(ch, { min_interval_s: '5s' }).errors.min_interval_s.code === 'number');
  check('channel: max age 0 refused', M.readChannel(ch, { max_age_s: '0' }).errors.max_age_s.code === 'maxAge');
  const own = M.channels([gx('ess_setpoint', { channel_override: { min_interval_s: 5 } })])[0];
  check('channel: the device\'s value is shown', M.channelShown(own).min_interval_s === '5');
  check('channel: emptied removes it (null)', eq(M.readChannel(own, { min_interval_s: '' }).payload, { min_interval_s: null }));
  check('channel: a stored value below the template still warns', !!M.readChannel(own, {}).below);
  const motor = M.channels(vfd)[0];
  check('channel: starts per hour must be whole', M.readChannel(motor, { max_starts_per_h: '2.5' }).errors.max_starts_per_h.code === 'whole');
}
// binding
{
  const c = gx('ess_setpoint');
  check('binding: an alias has none', !M.editable(vfd[1]));
  check('binding: nothing to release, none', !M.editable(vfd[2]));
  check('binding: a command with a value has one', M.editable(c));
  check('binding: safe on the value param', eq(M.safeKeys(c), ['value']));
  check('binding: a template safe names its params', eq(M.safeKeys({ params: { value: {}, revert_s: {} }, template_safe: { value: 100, revert_s: 0 } }), ['value', 'revert_s']));
  const no = M.readBinding(c, { lease_s: '90' });
  check('binding: a lease without any safe value is refused', no.errors.lease_s && no.errors.lease_s.code === 'leaseNoSafe', no);
  const ok = M.readBinding(c, { lease_s: '90', safe: { value: '0' } });
  check('binding: lease + safe sent', eq(ok.payload, { lease_s: 90, safe: { value: 0 } }) && !Object.keys(ok.errors).length, ok);
  check('binding: a safe out of bounds refused', M.readBinding(c, { safe: { value: '60000' } }).errors['safe.value'].code === 'above');
  check('binding: a fractional lease refused', M.readBinding(c, { lease_s: '1.5' }).errors.lease_s.code === 'wholeRange');
  const run = vfd[0];
  check('binding: the template\'s safe is enough for a lease', !Object.keys(M.readBinding(run, { lease_s: '60' }).errors).length);
  const pl = { name: 'power_limit', from_template: true, params: { value: { min: 0, max: 100 }, revert_s: { min: 0, max: 65535 } },
               template_safe: { value: 100, revert_s: 0 }, safe: { value: 100, revert_s: 0 }, safe_from: 'template', lease_s: 0 };
  const half = M.readBinding(pl, { safe: { value: '50' } });
  check('binding: a safe typed in part keeps the template\'s other params', eq(half.payload.safe, { value: 50, revert_s: 0 }), half.payload);
  const stored = { ...c, lease_s: 90, safe: { value: 0 }, safe_from: 'device' };
  check('binding: the stored values shown', eq(M.bindingShown(stored), { lease_s: '90', safe: { value: '0' } }));
  check('binding: emptying the safe sends null (the template\'s again)', eq(M.readBinding(stored, { safe: { value: '' } }).payload, { safe: null }));
  check('binding: ...and is refused while the lease stays', M.readBinding(stored, { safe: { value: '' } }).errors.lease_s?.code === 'leaseNoSafe');
  const legacy = { ...c, lease_s: 90 };
  const lr = M.readBinding(legacy, {});
  check('binding: a stored lease without safe is said, not blocking', lr.note === 'leaseNoSafe' && !Object.keys(lr.errors).length && !lr.dirty, lr);
}
// the group editor
{
  const cmds = [
    { ...gx('ess_setpoint'), binding: { name: 'ess_setpoint', enabled: true, faces: { api: true, mqtt: true, ha: false } } },
    { ...gx('min_soc'), binding: null },
    { ...gx('max_feed_in'), binding: { name: 'max_feed_in', enabled: true, lease_s: 30, safe: { value: 0 } }, lease_s: 30, safe: { value: 0 }, safe_from: 'device' },
  ];
  const r = M.groupCommands(cmds, {
    ess_setpoint: { ticked: true, typed: { lease_s: '90', safe: { value: '0' } } },
    min_soc: { ticked: false, typed: { lease_s: '', safe: { value: '' } } },
    max_feed_in: { ticked: false, typed: { lease_s: '30', safe: { value: '0' } } },
  });
  const by = Object.fromEntries(r.commands.map(b => [b.name, b]));
  check('group: a ticked binding keeps its faces and takes the fields', eq(by.ess_setpoint, { name: 'ess_setpoint', enabled: true, faces: { api: true, mqtt: true, ha: false }, lease_s: 90, safe: { value: 0 } }), by.ess_setpoint);
  check('group: an unticked command with nothing set is dropped', !by.min_soc);
  check('group: an unticked one that says something is kept, off', by.max_feed_in && by.max_feed_in.enabled === false && by.max_feed_in.lease_s === 30, by.max_feed_in);
  const bad = M.groupCommands(cmds, { min_soc: { ticked: true, typed: { lease_s: '60', safe: { value: '' } } } });
  check('group: lease without safe refused there too', bad.errors.min_soc && bad.errors.min_soc.lease_s.code === 'leaseNoSafe');
  const chs = M.channels(cmds);
  const g = M.groupChannels({ ess_setpoint: { min_interval_s: 10 }, gone: { min_interval_s: 1 } }, chs,
                            { ess_setpoint: { min_interval_s: '5' }, min_soc: { min_interval_s: '' } });
  check('group: channels typed over the stored, orphans kept', eq(g.channels, { ess_setpoint: { min_interval_s: 5 }, gone: { min_interval_s: 1 } }), g.channels);
  const own = M.channels([{ ...cmds[0], channel_override: { min_interval_s: 10 } }]);
  const cleared = M.groupChannels({ ess_setpoint: { min_interval_s: 10 } }, own, { ess_setpoint: { min_interval_s: '' } });
  check('group: an emptied channel leaves the overrides', eq(cleared.channels, {}), cleared.channels);
}

console.log(`${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
