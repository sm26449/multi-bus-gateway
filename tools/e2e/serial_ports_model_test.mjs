// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The serial port picker's model (ui/js/serial-ports.js), with no browser
// (ADR-0028): which port the connection form may pick for what it does
// (poll or listen), why one it may not is refused, and what it stores.
//
//   node tools/e2e/serial_ports_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const SP = require('../../ui/js/serial-ports.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};

// what GET /api/serial-ports answers (the Pi 5 bench)
const BY_ID = '/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B0045K08-if00-port0';
const ftdi = { name: 'ttyUSB1', dev: '/dev/ttyUSB1', path: '/dev/ttyUSB1', stable_path: BY_ID, by_id: [BY_ID],
  kind: 'usb', model: 'FT232R USB UART', state: 'free', polled_by: [], listened_by: [], processes: [], mapped: true };
const sc0 = { name: 'ttySC0', dev: '/dev/ttySC0', path: '/dev/ttySC0', stable_path: '/dev/ttySC0', kind: 'hat',
  label: 'SC16IS752 ch0 (spi0.0)', state: 'listened', listened_by: ['seplos-adr1', 'seplos-adr2'], polled_by: [], mapped: true };
const sc1 = { name: 'ttySC1', dev: '/dev/ttySC1', path: '/dev/ttySC1', stable_path: '/dev/ttySC1', kind: 'hat',
  state: 'polled', polled_by: ['deye'], listened_by: [], mapped: true };
const ama = { name: 'ttyAMA0', dev: '/dev/ttyAMA0', path: '/dev/ttyAMA0', stable_path: '/dev/ttyAMA0', kind: 'board_uart',
  state: 'not_mapped', polled_by: [], listened_by: [], mapped: false,
  fix: { docker_run: '--device /dev/ttyAMA0:/dev/ttyAMA0', compose: '- /dev/ttyAMA0:/dev/ttyAMA0' } };
const busy = { name: 'ttyUSB0', path: '/dev/ttyUSB0', stable_path: '/dev/ttyUSB0', kind: 'usb', state: 'busy',
  processes: ['gpsd (pid 4242)'], polled_by: [], listened_by: [], mapped: true };
const unk = { name: 'ttyS0', path: '/dev/ttyS0', stable_path: '/dev/ttyS0', kind: 'board_uart', state: 'unknown',
  polled_by: [], listened_by: [], mapped: true };

// what the form does on the bus
check('direct RTU polls', SP.role('rtu') === 'poll');
check('a tap listens', SP.role('rtu_tap') === 'listen');
check('Seplos V2 listen mode listens', SP.role('seplos_v2', 'listen') === 'listen');
check('Seplos V2 poll mode polls', SP.role('seplos_v2', 'poll') === 'poll');

// what is stored: the by-id link for a USB adapter
check('a USB adapter stores its by-id link', SP.value(ftdi) === BY_ID);
check('a HAT channel stores its path', SP.value(sc0) === '/dev/ttySC0');
check('an old answer (dev only) stores dev', SP.value({ dev: '/dev/ttyUSB3' }) === '/dev/ttyUSB3');
check('the stored value matches by path, by-id or dev',
  SP.matches(ftdi, BY_ID) && SP.matches(ftdi, '/dev/ttyUSB1') && !SP.matches(ftdi, '/dev/ttyUSB0'));
check('nothing matches an empty value', !SP.matches(ftdi, ''));

// free and listened
let c = SP.choice(ftdi, { role: 'poll' });
check('a free port can be polled', c.pickable && c.why === '');
c = SP.choice(sc0, { role: 'poll' });
check('polling a listened port is refused, with who listens',
  !c.pickable && c.why === 'listened' && c.who.join() === 'seplos-adr1,seplos-adr2', c);
c = SP.choice(sc0, { role: 'listen' });
check('another listener may join a listened port', c.pickable, c);
c = SP.choice(sc1, { role: 'listen' });
check('listening on a polled port is refused, with who polls', !c.pickable && c.why === 'polled' && c.who.join() === 'deye', c);
c = SP.choice(sc1, { role: 'poll' });
check('another slave may be polled on a polled bus', c.pickable && c.why === 'polled', c);

// the device being edited is not its own conflict
c = SP.choice(sc0, { role: 'poll', self: 'seplos-adr1' });
check('one other listener still refuses', !c.pickable && c.who.join() === 'seplos-adr2', c);
c = SP.choice({ ...sc0, listened_by: ['me'] }, { role: 'poll', self: 'me' });
check('a device that listened alone may become the poller', c.pickable && c.why === '', c);

// what the container cannot use
c = SP.choice(ama, { role: 'poll' });
check('a port not mapped is refused, with the line to add',
  !c.pickable && c.why === 'not_mapped' && c.fix.compose === '- /dev/ttyAMA0:/dev/ttyAMA0', c);
c = SP.choice(busy, { role: 'listen' });
check('a port another program holds is refused, with who', !c.pickable && c.why === 'busy' && c.who.join() === 'gpsd (pid 4242)', c);
c = SP.choice(unk, { role: 'poll' });
check('an unknown port can be picked, with the doubt said', c.pickable && c.why === 'unknown', c);
c = SP.choice({ ...unk, state: 'console' }, { role: 'poll' });
check('the console is never picked', !c.pickable && c.why === 'console', c);
c = SP.choice({ dev: '/dev/ttyUSB3' }, { role: 'poll' });
check('an old answer (no state) is pickable', c.pickable, c);

// production (review B1): the container has the Fronius adapter only as the
// udev alias /dev/ttyFRONIUS; sysfs calls it ttyUSB0
const fr = { name: 'ttyUSB0', dev: '/dev/ttyUSB0', path: '/dev/ttyUSB0', stable_path: '/dev/ttyFRONIUS',
  aliases: ['/dev/ttyFRONIUS'], by_id: [], kind: 'usb', model: 'FT232R USB UART', state: 'polled',
  base_state: 'free', polled_by: ['fronius'], listened_by: [], mapped: true };
check('an alias is what is stored', SP.value(fr) === '/dev/ttyFRONIUS');
check('the stored alias marks the row current', SP.matches(fr, '/dev/ttyFRONIUS'));
check('an alias wins over an absent node even without stable_path',
  SP.value({ ...fr, stable_path: '' }) === '/dev/ttyFRONIUS');
c = SP.choice(fr, { role: 'poll', self: 'fronius' });
check('editing the owner: pickable, free, through the alias', c.pickable && c.why === '', c);
c = SP.choice({ ...fr, mapped: false, base_state: 'not_mapped', aliases: [] }, { role: 'poll', self: 'fronius' });
check('its own claim never turns an unmapped port free', !c.pickable && c.why === 'not_mapped', c);
c = SP.choice({ ...sc0, base_state: 'busy', processes: ['gpsd'] }, { role: 'listen' });
check('a joinable listened port another program holds is still refused', !c.pickable && c.why === 'busy', c);

// a renamed mapping (compose /dev/ttyUSB1:/dev/ttyUSB0): the host's ttyUSB0
// is not here, and /dev/ttyUSB0 here is another device
const taken = { name: 'ttyUSB0', dev: '/dev/ttyUSB0', path: '/dev/ttyUSB0', stable_path: '', path_taken: true,
  aliases: [], by_id: [], kind: 'usb', state: 'not_mapped', base_state: 'not_mapped', mapped: false,
  polled_by: [], listened_by: [] };
check('a name another device holds never matches', !SP.matches(taken, '/dev/ttyUSB0'));
check('a name another device holds is never a value', SP.value(taken) === '');
check('the device that has that name matches it',
  SP.matches({ ...fr, aliases: ['/dev/ttyUSB0'], stable_path: '/dev/ttyUSB0', name: 'ttyUSB1', path: '/dev/ttyUSB1' }, '/dev/ttyUSB0'));

// what the list shows
check('the label says the port and the model', SP.label(ftdi) === 'ttyUSB1 · FT232R USB UART');
check('a HAT channel shows the chip', SP.label(sc0) === 'ttySC0 · SC16IS752 ch0 (spi0.0)');
check('kinds are named', SP.kind(ftdi) === 'usb' && SP.kind(sc0) === 'hat' && SP.kind(ama) === 'board_uart'
  && SP.kind({}) === 'other');

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
